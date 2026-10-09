# Debt Risk Radar

Dashboard Streamlit de monitoring du risque lie a la dette souveraine, aux taux, au credit prive et a la liquidite.

L'app est centree sur les Etats-Unis. La methode `us-debt-institutional` version `2.0`
repose sur Treasury, FRED, BIS et World Bank : 31 signaux courants requis, plus 4
projections CBO structurelles separees. Elle ne collecte plus de prix d'ETF Massive.
Ce changement rompt la comparabilite avec l'ancien score incluant les ETF.

La politique de fraicheur 2 distingue une publication trimestrielle FRED differee
d'une panne de collecte : confirmation officielle recente, bornes d'age explicites,
periode d'origine preservee et avertissement visible. Aucun seuil n'est prolonge
sans cette verification. Voir [METHODOLOGY.md](METHODOLOGY.md) et [API.md](API.md).

Documentation : [methode et poids](METHODOLOGY.md), [contrat JSON](API.md),
[deploiement et migration](DEPLOYMENT.md), [securite](SECURITY.md).

## Ce que surveille l'app

- Dette publique US quotidienne via Treasury Fiscal Data.
- Dette publique / PIB, dette detenue par le public / PIB, deficit et interets federaux via FRED.
- Courbe des taux, breakevens, spreads investment grade et high yield via FRED.
- Dette et fragilite privee via FRED.
- Indicateurs annuels comparables via World Bank.
- Credit-to-GDP gap et debt service ratios via BIS.
- Projections CBO long terme : dette detenue par le public, dette brute, deficit, interets.
- Scenario `r-g` pour tester la trajectoire dette / PIB.

## Installation

```bash
git clone https://github.com/bluetouff/debt-risk-radar.git
cd debt-risk-radar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Treasury Fiscal Data, BIS, CBO et World Bank fonctionnent sans cle API.

FRED exige une cle API gratuite cote serveur. Ses 19 signaux sont requis pour
calculer le score courant : sans cette cle, les autres sources restent lisibles
mais le score est indisponible.

```bash
export FRED_API_KEY="ta_cle_fred"
```

En production, renseigner la cle avec `sudoedit /etc/debt-risk-radar.env`, jamais
dans l'historique shell. Le dashboard et l'exporteur ignorent les anciens reglages
`MASSIVE_*`. Leur presence n'active aucun appel ni signal ETF.

L'acces gratuit n'accorde pas automatiquement des droits de redistribution commerciale.
Les series FRED tierces, notamment ICE BofA, conservent leurs restrictions propres.
Voir les [conditions FRED](https://fred.stlouisfed.org/docs/api/terms_of_use.html)
et la section droits de [METHODOLOGY.md](METHODOLOGY.md).

## Lancement

```bash
streamlit run app.py
```

L'app demarre sur `http://localhost:8501`.

En local comme en prod, la configuration Streamlit fournie force l'ecoute sur `127.0.0.1`, desactive la telemetrie et masque les details d'erreur cote client.

## Export machine-readable

En production, le collecteur planifie ecrit un snapshot public dans :

```text
/var/www/debt-risk-radar/latest.json
```

Apache sert ce fichier sur :

```text
https://debt.l0g.fr/latest.json
```

Le JSON expose le score de stress courant, les scores par famille, les principaux signaux, les sources chargees, les seuils et les flux manquants. Il ne contient jamais de cle API.
Il est genere par `latest_export.py` et rafraichi par un timer systemd dedie, sans dependance a une visite navigateur.

Le schema `1.2` conserve les champs du schema 1.1 et ajoute `methodology`, les poids
courants normalises et les compteurs du score. Le perimetre passe de 44 a 35 signaux,
dont 31 courants. Le score courant est `null` si sa couverture est
incomplete : aucune valeur manquante n'est remplacee par 50. Un consommateur doit
verifier la version de methode, `valid_until`, `quality` et `score.coverage` avant
d'utiliser le score. Ne pas raccorder automatiquement les deux methodes dans un historique.

`collection.status` decrit le renouvellement des sources, independamment de la qualite
des observations encore valides. La valeur `paused` accompagne une liste `providers`
avec `source`, `reason`, `last_attempt_at` et `retry_at` (UTC). Une pause ne rend pas
perimee une reponse deja collectee : elle reste utilisable jusqu'a son echeance initiale.
`unknown` signifie que le diagnostic du collecteur n'est pas disponible. Les motifs
sont des codes controles, sans URL de requete, cle API ni contenu de reponse.

Les requetes sont mises en cache sur disque pendant six heures pour Treasury/FRED,
et vingt-quatre heures pour BIS/CBO/World Bank. Le collecteur commence leur renouvellement
dans les trente dernieres minutes de validite pour eviter un trou entre deux passages ;
cette anticipation ne prolonge jamais leur TTL. Si le renouvellement echoue, la reponse
precedente est conservee tant qu'elle reste valide, y compris apres l'attente reseau.
Son horodatage initial ne change pas. Les redemarrages du collecteur ne vident
pas ce cache. En production, l'application publique lit uniquement le cache ; les visites
ne declenchent aucun appel aux fournisseurs. Les echecs et quotas declenchent une pause
persistante par fournisseur, sans retry immediat. Voir `DEPLOYMENT.md` pour les services.
Une reponse HTTP 429 impose
au moins 15 minutes de pause ; des refus consecutifs doublent progressivement cette
pause jusqu'a six heures, sans jamais raccourcir un `Retry-After` plus long.
Les pauses des anciens fournisseurs inactifs ne sont plus publiees dans `collection`.
Leurs donnees de cache et de backoff sont conservees pour permettre un retour arriere.

## Structure

```text
debt-risk-radar/
├── app.py             # UI Streamlit
├── catalog.py         # Series, sources, poids, directions de risque
├── data.py            # Connecteurs, normalisation, scoring, scenarios
├── latest_export.py   # Generation du snapshot public latest.json
├── METHODOLOGY.md     # Methode 2.0, poids et limites
├── API.md             # Contrat JSON 1.2 et regles consommateurs
├── DEPLOYMENT.md      # Runbook Debian + Apache + systemd durci
├── SECURITY.md        # Modele de securite et checklist
├── scripts/           # Checks locaux, dont scan anti-secrets
├── deploy/            # Unit systemd, vhost Apache, env example
├── requirements.txt   # Dependances
└── README.md
```

## Scoring

Les series FRED et les niveaux Treasury utilisent une fenetre de cinq ans,
World Bank et les ratios BIS dix ans et le CBO trente ans.
Certains signaux utilisent aussi des seuils de niveau, notamment le credit gap,
la croissance de dette et les projections CBO. Le z-score est signe selon le sens du risque :

- `direction = up` : une hausse augmente le risque.
- `direction = down` : une baisse augmente le risque.

Le score est transforme sur une echelle 0-100 :

```text
risk_score = clip(50 + signed_z * 15, 0, 100)
```

Les buckets courants sont ensuite agreges avec des poids. Les projections CBO long terme sont conservees
comme indicateur structurel separe : elles sont affichees et exportees, mais exclues du score de stress
courant parce qu'elles ne mesurent pas un choc de marche actuel.
Une famille incomplete n'a pas de score agrege. Le score courant exige toutes les
familles courantes completes. Les observations valides restent affichees individuellement.
Les coefficients courants ci-dessous sont normalises par leur somme (0,86), hors CBO.
Ce sont des coefficients de base, pas les pourcentages effectifs du nouveau score.
Les pourcentages effectifs sont detailles dans [METHODOLOGY.md](METHODOLOGY.md).

- Fiscal solvency : 0,22
- Rates and market stress : 0,18
- Private leverage : 0,12
- Liquidity plumbing : 0,10
- Treasury daily debt : 0,10
- Global comparables : 0,04 (USA uniquement)
- BIS global credit : 0,10 (USA uniquement)
- CBO projections : coefficient historique 0,10, poids effectif courant nul

Seuils d'affichage :

- moins de 50 : Calm
- 50 a moins de 65 : Elevated
- 65 a moins de 80 : Watch
- 80 et plus : Stress

## Sources actives

- US Treasury Fiscal Data, `Debt to the Penny`
- FRED / St. Louis Fed
- BEA via FRED pour les interets federaux
- World Bank Indicators API
- BIS Data Portal bulk downloads : `WS_CREDIT_GAP`, `WS_DSR`
- CBO Open Data GitHub : `long_term_budget`

## Roadmap

- Ajouter BIS total credit (`WS_TC`) en complement du credit gap.
- Ajouter CBO ten-year budget pour rapprocher projections 10 ans et long terme.
- Ajouter SEC EDGAR pour dette corporate, maturites et interest expense.
- Ajouter alertes email ou webhook sur franchissement de seuils.
- Ajouter persistance DuckDB pour historiser les snapshots et calculer des revisions.

## Production

Lis [DEPLOYMENT.md](DEPLOYMENT.md) et [SECURITY.md](SECURITY.md) avant exposition publique.

Principes non negociables :

- Streamlit ecoute uniquement sur `127.0.0.1`.
- Apache expose HTTPS et les websockets `_stcore`.
- Apache sert `/latest.json` comme fichier statique public, hors proxy Streamlit.
- Les secrets restent dans `/etc/debt-risk-radar.env`, jamais dans le repo.
- Le hook Git local pointe vers `.githooks` pour bloquer les secrets avant commit.
- Le service tourne avec l'utilisateur systeme `debt-radar`, pas root.
- Le port applicatif local, `8502` en production, reste ferme depuis Internet.

Active les hooks locaux une fois par clone :

```bash
git config core.hooksPath .githooks
```

## Licence

MIT. Voir [LICENSE](LICENSE).
