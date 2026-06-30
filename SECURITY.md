# Security model

## Principes

- L'application n'ecoute qu'en local : `127.0.0.1:8502`.
- Apache termine TLS et expose le service au public.
- Apache sert `/latest.json` comme fichier statique public genere par l'app.
- Les cles API sont lues depuis l'environnement serveur ou les secrets Streamlit.
- Les erreurs upstream sont redigees avant affichage pour eviter les fuites de secrets.
- Massive Market Data est appele avec `Authorization: Bearer`, jamais avec une cle en query string.

## Donnees sensibles

Secrets attendus :

- `FRED_API_KEY`
- `MASSIVE_API_KEY`
- eventuellement `MASSIVE_BASE_URL`

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
- `api.stlouisfed.org` via `fredapi`
- `api.massive.com` ou `MASSIVE_BASE_URL`

## Durcissement serveur

Utiliser le service systemd fourni :

- `NoNewPrivileges=true`
- `ProtectSystem=strict`
- `ProtectHome=true`
- `PrivateTmp=true`
- `CapabilityBoundingSet=`
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`
- ecriture limitee a `/var/lib/debt-risk-radar` et `/var/www/debt-risk-radar`

## Avant exposition publique

- Verifier `curl -I https://domaine/`.
- Verifier que le port `8502` est ferme depuis Internet.
- Verifier `curl -sS https://domaine/latest.json | python3 -m json.tool`.
- Verifier les logs Apache et systemd apres une erreur volontaire de cle invalide.
- Verifier que les tableaux ne montrent pas de trace, exception brute ou secret.
