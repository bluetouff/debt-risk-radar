# Debt Risk Radar : deploiement Debian durci

Objectif : exposer Streamlit uniquement via Apache en HTTPS, sans jamais publier le port applicatif ni les cles API.

Les etapes 1-7 concernent une premiere installation. Pour un serveur existant,
suivre la section 8 ; ne pas recreer les secrets ni remplacer une configuration
Apache ou un pare-feu partage sans examiner les differences.

## 1. Utilisateur systeme

```bash
sudo adduser --system --group --home /var/lib/debt-risk-radar debt-radar
sudo install -d -o debt-radar -g debt-radar -m 700 /var/lib/debt-risk-radar
sudo install -d -o debt-radar -g debt-radar -m 700 /var/lib/debt-risk-radar/cache
sudo install -d -o debt-radar -g debt-radar -m 755 /var/www/debt-risk-radar
sudo install -d -o debt-radar -g debt-radar -m 755 /opt/debt-risk-radar
```

## 2. Code et venv

```bash
export DEBT_RISK_RADAR_SRC="$HOME/debt-risk-radar"

sudo rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude .env \
  --exclude .streamlit/secrets.toml \
  "$DEBT_RISK_RADAR_SRC"/ /opt/debt-risk-radar/

sudo chown -R root:root /opt/debt-risk-radar
sudo python3 -m venv /opt/debt-risk-radar/.venv
sudo /opt/debt-risk-radar/.venv/bin/pip install --upgrade pip
sudo /opt/debt-risk-radar/.venv/bin/pip install -r /opt/debt-risk-radar/requirements.txt
```

## 3. Secrets

```bash
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar.env.example /etc/debt-risk-radar.env
sudoedit /etc/debt-risk-radar.env
sudo chown root:debt-radar /etc/debt-risk-radar.env
sudo chmod 640 /etc/debt-risk-radar.env
```

Ne mets jamais `FRED_API_KEY` ni une ancienne cle Massive dans le code, les logs,
l'historique shell ou Apache. La methode 2.0 n'utilise que la cle FRED ; les anciennes
variables `MASSIVE_*` ne reactivent pas la collecte ETF.

## 4. Service systemd

```bash
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar.service /etc/systemd/system/debt-risk-radar.service
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar-export.service /etc/systemd/system/debt-risk-radar-export.service
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar-export.timer /etc/systemd/system/debt-risk-radar-export.timer
sudo systemctl daemon-reload
sudo systemctl start debt-risk-radar-export.service
sudo systemctl enable --now debt-risk-radar-export.timer
sudo systemctl enable --now debt-risk-radar
sudo systemctl status debt-risk-radar
```

Le service ecoute uniquement sur `127.0.0.1:8502`.

Le fichier machine-readable public est ecrit dans `/var/www/debt-risk-radar/latest.json` par
`debt-risk-radar-export.service`, puis rafraichi par `debt-risk-radar-export.timer`.

## 5. Apache reverse proxy

Modules requis :

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel headers ssl rewrite
sudo cp /opt/debt-risk-radar/deploy/apache-debt-risk-radar.conf /etc/apache2/sites-available/debt-risk-radar.conf
sudo a2ensite debt-risk-radar
sudo apache2ctl configtest
sudo systemctl reload apache2
```

Adapte `ServerName` et les chemins Let's Encrypt dans le fichier Apache.

Le vhost exclut `/latest.json` du reverse proxy Streamlit et le sert directement depuis
`/var/www/debt-risk-radar/latest.json`.

## 6. Pare-feu

Exemple pour une machine neuve, apres verification du port SSH et d'un acces de
secours. Ne pas appliquer ce bloc sur un serveur deja protege par port-knocking
ou avec des regles partagees : conserver la politique existante.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow "Apache Full"
sudo ufw enable
```

Le port `8502` ne doit jamais etre ouvert publiquement.

## 7. Verification

```bash
curl -I https://debt.l0g.fr/
curl -sS https://debt.l0g.fr/latest.json | python3 -m json.tool | head
curl -sS http://127.0.0.1:8502/_stcore/health
sudo journalctl -u debt-risk-radar -n 100 --no-pager
sudo journalctl -u debt-risk-radar-export -n 100 --no-pager
```

Controle attendu :

- HTTPS force.
- `X-Frame-Options: DENY`.
- `X-Content-Type-Options: nosniff`.
- `Referrer-Policy: no-referrer`.
- `/latest.json` repond en JSON valide, sans cle API.
- aucune cle API dans les logs.
- service lance sous `debt-radar`, pas root.

## 8. Mise a jour

Une activation doit etre explicitement autorisee. Un commit pousse ou un healthcheck
HTTP reussi ne prouve ni la version active ni la validite des donnees.

### Preparation de la methode 2.0

Pour la politique de fraicheur 3, deployer ensemble `http_cache.py`, `data.py`,
`source_validation.py`, `quality.py`, `latest_export.py` et `app.py`, puis le
consommateur l0g qui affiche `official-delayed` et `cached`. Ne pas effacer le cache
ni provoquer une collecte manuelle. L'ancien cache est revalide a la lecture.
Le prochain passage normal peut demander deux metadonnees FRED supplementaires
si les deux ratios dette/PIB approchent leur limite. Elles sont ensuite cachees
avec renouvellement 6 h et limite maximale 48 h, avec les memes pauses fournisseur.
Verifier `quality.policy_version`, `cached_signals`, `delayed_signals`, les dates
`publication_*`, `observation_checked_at` et `valid_until`, pas uniquement HTTP 200.

1. Verifier le checkout propre, le SHA attendu et le diff. Ne pas ecraser des modifications locales.
2. Executer les tests hors reseau dans le repertoire de la release :
   `python -B -m unittest discover -s tests -v`. Utiliser le chemin absolu des tests
   si le script d'activation est lance depuis un autre repertoire.
3. Scanner les secrets et verifier le manifeste de fichiers de la release, y compris
   `quality.py`, `http_cache.py`, les tests et les nouvelles documentations.
4. Examiner les consommateurs l0g : ils doivent conserver `methodology.id/version`,
   marquer la rupture de serie et ne pas reemployer l'ancien score comme repli 2.0.
   Leur adaptation n'est pas faite par le deploiement de ce producteur.
5. Sauvegarder le code actuellement actif, son SHA et son manifeste dans un dossier
   prive horodate de `/var/backups/debt-risk-radar`. Ne pas inclure de secrets dans
   un artefact public. Conserver une copie de l'ancien JSON avec sa date et sa methode.

### Activation bornee

Arreter temporairement le timer et attendre la fin du collecteur avant de remplacer
les fichiers : ne pas melanger deux versions au cours d'un export. Le script
d'activation doit restaurer la release precedente si une verification echoue.

Synchroniser uniquement la release verifiee. Preserver `.venv`, `.streamlit`, `.env`,
`/etc/debt-risk-radar.env` et tout `/var/lib/debt-risk-radar`, notamment le cache et les
pauses fournisseurs. Garder le code root:root et le repertoire applicatif en 755.
Ne pas recopier `deploy/debt-risk-radar.env.example` sur les secrets existants.

Cette migration ne change ni les dependances, ni Apache, ni les services systemd.
Comparer les fichiers deployes avant toute copie de configuration ; aucun reload
Apache, ouverture de port ou affaiblissement du sandbox n'est necessaire.
Redemarrer seulement `debt-risk-radar`, puis reactiver le timer et attendre son
passage naturel. Ne pas vider le cache ni forcer plusieurs collectes.

### Preuves apres activation

- Verifier le SHA deploye et les empreintes des fichiers, pas seulement `git HEAD` du clone.
- Verifier l'application active, l'ecoute `127.0.0.1:8502` et les healthchecks local/HTTPS.
- Apres le passage du timer, verifier `/latest.json` en HTTPS : schema `1.2`, methode
  `us-debt-institutional` / `2.0`, 35 signaux audites, 31 courants attendus, aucun bucket
  `market_prices`, aucun fournisseur Massive dans les sources actives.
- Exiger pour une collecte complete : `quality.eligible_signals = 35`,
  `score.eligible_signals = 31`, `score.coverage = 1`, score fini et `valid_until` futur.
  Sinon inspecter les signaux institutionnels manquants ; ne pas relacher les seuils.
- Verifier dans le navigateur la mention methode 2.0, la FAQ, le KPI taux/credit et
  le graphique FRED sans prix ETF. Verifier aussi la carte l0g et sa provenance.
- Verifier un renouvellement naturel des caches actifs, et pas seulement un premier
  export reussi. Une ancienne pause Massive ne doit produire aucun nouvel appel.
- Observer au moins 48 h couvrant renouvellements, changement de jour et redemarrage
  planifie avant de conclure a une stabilite observee. Les simulations hors reseau
  de six jours de panne, expiration au septieme jour, reponse invalide et reprise
  sont des preuves de comportement du code, pas une preuve de disponibilite en production.

### Retour arriere

Restaurer exactement la release sauvegardee, avec son manifeste, puis redemarrer
l'application et remettre le timer dans son etat initial. Ne pas effacer les caches,
les secrets ou les pauses. La methode precedente etait dependante de Massive : son
retour peut retablir l'indisponibilite liee au quota. Ne pas presenter le JSON 2.0
comme issu de l'ancienne release, ni redater un ancien JSON pour le rendre utilisable.
Attendre un export coherent avec la release active et verifier son expiration.

## Notes securite

- Verifier `methodology`, `quality`, `score.coverage`, `signals` et `valid_until` apres la premiere collecte.
- La methode 2.0 suspend `score.current_stress` (`null`) si un des 31 signaux courants manque ; aucune imputation a 50.
- Le cache persiste : renouvellement 6 h Treasury/FRED, 24 h BIS/CBO/World Bank ;
  reutilisation maximale 48 h Treasury/FRED, 7 jours BIS/World Bank, 30 jours CBO fige.
- Un second export ne doit declencher aucun appel fournisseur tant que le cache
  n'est pas dans les trente dernieres minutes de son TTL. Le renouvellement anticipe
  ne modifie pas la date d'observation ni la limite maximale d'une reponse.
- Ne pas vider le cache ou lancer des collectes repetees pour contourner un HTTP 429 :
  la pause persiste et augmente si le fournisseur continue a refuser les appels.
  Le retrait de Massive ne modifie pas les pauses des fournisseurs institutionnels.
- Si le score courant est indisponible, le collecteur publie le JSON degrade et sort avec le code 2.
  Inspecter `quality.unavailable_signals` et le journal avant de poursuivre la bascule.
- Une pause de renouvellement peut coexister avec une couverture complete : verifier
  `collection.status` et `collection.providers` pour la cause et `retry_at` en UTC.
  Le cache conserve son echeance initiale, y compris apres un echec de renouvellement.
  Le journal publie les identifiants des signaux indisponibles et les pauses actives.
  Ne pas assimiler un seul passage reussi a une resolution durable : verifier aussi
  le prochain renouvellement des sources concernees, sans forcer leurs appels.

- Streamlit reste une app serveur : garde-la derriere Apache, jamais exposee directement.
- Garde `showErrorDetails=false` en production.
- La CSP est volontairement compatible Streamlit. Tu peux la durcir apres test navigateur complet, mais ne casse pas les websockets `_stcore`.
- Le systemd fourni bloque l'ecriture partout sauf `/var/lib/debt-risk-radar` et `/var/www/debt-risk-radar`.
