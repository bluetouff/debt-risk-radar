# Debt Risk Radar

Dashboard Streamlit de monitoring du risque lie a la dette souveraine, aux taux, au credit prive et a la liquidite.

L'app est centree sur les Etats-Unis, parce que les sources ouvertes y sont les plus riches et les plus rapides a exploiter. La V2 ajoute BIS, CBO et Massive Market Data.

## Ce que surveille l'app

- Dette publique US quotidienne via Treasury Fiscal Data.
- Dette publique / PIB, dette detenue par le public / PIB, deficit et interets federaux via FRED.
- Courbe des taux, breakevens, spreads investment grade et high yield via FRED.
- Dette et fragilite privee via FRED.
- Indicateurs annuels comparables via World Bank.
- Credit-to-GDP gap et debt service ratios via BIS.
- Projections CBO long terme : dette detenue par le public, dette brute, deficit, interets.
- Prix et ratios de marche via Massive Market Data.
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

FRED est optionnel mais fortement recommande :

```bash
export FRED_API_KEY="ta_cle_fred"
```

Massive Market Data est optionnel mais requis pour les prix et ratios de marche :

```bash
export MASSIVE_API_KEY="ta_cle_massive"
export MASSIVE_BASE_URL="https://api.massive.com"
```

La cle Massive est envoyee en header `Authorization: Bearer`, jamais en query string.

Note methodologique importante : pour les prix de marche, ratios financiers, ETF, actions, crypto ou actifs tradables, utilise Massive Market Data en source primaire pour les prix et ratios. Garde FRED, Treasury, BEA, BLS, BIS, SEC EDGAR, CBO, World Bank et IMF pour les donnees institutionnelles.

## Lancement

```bash
streamlit run app.py
```

L'app demarre sur `http://localhost:8501`.

En local comme en prod, la configuration Streamlit fournie force l'ecoute sur `127.0.0.1`, desactive la telemetrie et masque les details d'erreur cote client.

## Export machine-readable

En production, le dashboard ecrit un snapshot public dans :

```text
/var/www/debt-risk-radar/latest.json
```

Apache sert ce fichier sur :

```text
https://debt.l0g.fr/latest.json
```

Le JSON expose le score de stress courant, les scores par famille, les principaux signaux, les sources chargees, les seuils et les flux manquants. Il ne contient jamais de cle API.
Il est genere par `latest_export.py` et rafraichi par un timer systemd dedie, sans dependance a une visite navigateur.

## Structure

```text
debt-risk-radar/
├── app.py             # UI Streamlit
├── catalog.py         # Series, sources, poids, directions de risque
├── data.py            # Connecteurs, normalisation, scoring, scenarios
├── latest_export.py   # Generation du snapshot public latest.json
├── DEPLOYMENT.md      # Runbook Debian + Apache + systemd durci
├── SECURITY.md        # Modele de securite et checklist
├── scripts/           # Checks locaux, dont scan anti-secrets
├── deploy/            # Unit systemd, vhost Apache, env example
├── requirements.txt   # Dependances
└── README.md
```

## Scoring

Chaque serie est convertie en z-score sur fenetre mobile de 5 ans, signe selon la direction de risque :

- `direction = up` : une hausse augmente le risque.
- `direction = down` : une baisse augmente le risque.

Le score est transforme sur une echelle 0-100 :

```text
risk_score = clip(50 + signed_z * 15, 0, 100)
```

Les buckets courants sont ensuite agreges avec des poids. Les projections CBO long terme sont conservees
comme indicateur structurel separe : elles sont affichees et exportees, mais exclues du score de stress
courant parce qu'elles ne mesurent pas un choc de marche actuel.

- Fiscal solvency : 22 %
- Rates and market stress : 18 %
- Private leverage : 12 %
- Liquidity plumbing : 10 %
- Treasury daily debt : 10 %
- Global comparables : 4 %
- BIS global credit : 10 %
- CBO projections : 10 %, structurel, exclu du score courant
- Massive market prices : 4 %

Seuils d'affichage :

- 65 : watch
- 80 : stress

## Sources V1

- US Treasury Fiscal Data, `Debt to the Penny`
- FRED / St. Louis Fed
- BEA via FRED pour les interets federaux
- World Bank Indicators API
- BIS Data Portal bulk downloads : `WS_CREDIT_GAP`, `WS_DSR`
- CBO Open Data GitHub : `long_term_budget`
- Massive Market Data : daily aggregates, Polygon-shaped REST

## Roadmap

- Ajouter BIS total credit (`WS_TC`) en complement du credit gap.
- Ajouter CBO ten-year budget pour rapprocher projections 10 ans et long terme.
- Ajouter SEC EDGAR pour dette corporate, maturites et interest expense.
- Ajouter davantage de tickers Massive : MOVE proxy, ETFs inflation-linked, banques, regional banks, CDS proxy si disponible.
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
