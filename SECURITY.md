# Security model

## Principes

- L'application n'ecoute qu'en local : `127.0.0.1:8502`.
- Apache termine TLS et expose le service au public.
- Apache sert `/latest.json` comme fichier statique public genere par un service systemd oneshot.
- Les cles API sont lues depuis l'environnement serveur ou les secrets Streamlit.
- Les erreurs upstream sont redigees avant affichage pour eviter les fuites de secrets.
- La methode 2.0 ne collecte plus Massive ni les neuf signaux ETF, meme si une ancienne cle est configuree.

## Donnees sensibles

Secret actif :

- `FRED_API_KEY`

Les anciens `MASSIVE_API_KEY` et `MASSIVE_BASE_URL` peuvent subsister pour un retour
arriere. Ils ne sont plus lus par le dashboard ou l'exporteur. Ne pas les imprimer
ni les purger automatiquement, et ne pas les inclure dans les artefacts publics.

Ces valeurs ne doivent pas etre committees, imprimees, copiees dans le navigateur ou ajoutees aux URLs.

`/latest.json` ne doit contenir que des scores, metadonnees de sources, dates et messages d'etat
rediges. Il ne doit jamais exposer l'environnement serveur, les headers HTTP sortants ou les cles.

## Garde-fous Git

- `.gitignore` exclut venv, caches, `.env`, `.streamlit/secrets.toml` et materiel TLS.
- `.githooks/pre-commit` lance `scripts/secret_scan.py --staged` et `git diff --cached --check`.
- Active le hook avec `git config core.hooksPath .githooks` apres chaque clone.
- Le scan local complete GitHub secret scanning, il ne le remplace pas.

## Surfaces reseau sortantes

Allowlist fonctionnelle :

- `api.fiscaldata.treasury.gov`
- `api.worldbank.org`
- `data.bis.org`
- `raw.githubusercontent.com` pour le depot officiel `US-CBO/cbo-data`
- `api.stlouisfed.org`

`api.massive.com` reste dans l'allowlist du connecteur historique, hors du chemin
d'execution public. Ce connecteur utilise `Authorization: Bearer`, jamais une cle
en query string. Les tests conservent ses controles de redaction, cache et quotas.
Les cinq hotes actifs sont declares dans `catalog.ACTIVE_SOURCE_HOSTS`.

Les lectures sortantes passent par `http_cache.py` : allowlist HTTPS, pas de redirection,
delais et tailles limites, cache SQLite prive sans cle API ni en-tete d'autorisation.
Les echecs ne contournent jamais le cache. Le collecteur renouvelle les reponses dans
les trente dernieres minutes de leur TTL (10 % pour un TTL plus court) ; les lecteurs
appliquent une limite maximale distincte de cette cadence (politique 3). Un renouvellement refuse est signale dans `collection`
et le journal avec un motif controle, sans contenu fournisseur ni secret. La reponse
precedente reste lisible uniquement si sa limite maximale n'a pas expire a la fin de la requete.
Une pause active n'empeche pas sa lecture, sans changer sa date de collecte.
Les reponses expirees ne sont
jamais reutilisees comme donnees courantes. Les erreurs HTTP respectent `Retry-After`
(au moins 15 minutes). Les echecs reseau, HTTP ou de validation consecutifs doublent la pause jusqu'a six heures,
avec un compteur persistant ; un `Retry-After` plus long reste prioritaire. Seule
une nouvelle reponse acceptee remet ce compteur a zero, pas une lecture du cache.
Les erreurs 401/403 suspendent les appels pendant six heures, les autres echecs
pendant au moins 15 minutes, puis selon ce backoff. Aucun retry immediat. Les appels Massive sont espaces d'au moins
65 secondes apres la fin de la requete precedente dans le connecteur historique,
non appele par la methode 2.0. Les metadonnees publiques de collecte filtrent ses
anciennes pauses sans effacer l'etat prive. Les pauses FRED restent visibles.

Le retrait des ETF ne relache aucun controle de qualite : le score courant exige
31 signaux institutionnels eligibles, sans donnees synthetiques, date rajeunie ni
imputation. La methode 2.0 et le schema JSON 1.2 sont explicites pour eviter une
comparaison silencieuse avec l'ancien score. Voir `METHODOLOGY.md` et `API.md`.
L'acces gratuit a FRED ne garantit pas les droits de redistribution des series tierces.

La politique de fraicheur 3 utilise les memes hotes autorises,
plafonds, verrous, pauses et redaction des secrets. `source_validation.py` valide
les formats, identifiants, dates, doublons et valeurs avant tout remplacement du cache.
Le JSON non fini est rejete. Les archives BIS gardent leur plafond de decompression.
Les dates de collecte sont celles de la reponse persistante,
jamais celles d'une lecture. Une confirmation doit correspondre a l'identifiant,
a la frequence et a la periode ; les dates futures et les bornes depassees sont
rejetees. Aucun nouvel appel reseau depuis l'application publique.

Le service public est en lecture seule du cache et ne peut plus ecrire `latest.json`.
Les restrictions IP systemd limitent ses connexions au loopback. Le collecteur planifie
est seul autorise a interroger les fournisseurs. Creer et remplir le cache avant
de demarrer l'application en lecture seule.

## Durcissement serveur

Utiliser le service systemd fourni :

- `NoNewPrivileges=true`
- `ProtectSystem=strict`
- `ProtectHome=true`
- `PrivateTmp=true`
- `CapabilityBoundingSet=`
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`
- ecriture limitee a `/var/lib/debt-risk-radar` et `/var/www/debt-risk-radar`

Le timer `debt-risk-radar-export.timer` rafraichit le JSON avec le meme utilisateur systeme
`debt-radar`, sans exposer Streamlit ni ajouter de port public.

## Avant exposition publique

- Verifier `curl -I https://domaine/`.
- Verifier que le port `8502` est ferme depuis Internet.
- Verifier `curl -sS https://domaine/latest.json | python3 -m json.tool`.
- Verifier les logs Apache et systemd apres une erreur volontaire de cle invalide.
- Verifier que les tableaux ne montrent pas de trace, exception brute ou secret.
