# Methode 2.0 : socle institutionnel US

Identifiant : `us-debt-institutional`. Version : `2.0`.
Cette version est definie dans `catalog.py`, independamment de la version du schema JSON.
Le code de calcul est dans `data.py` et les controles d'eligibilite dans `quality.py`.

## Perimetre et rupture de serie

Le score courant exige 31 signaux americains. Les 4 projections CBO sont affichees
separement, soit 35 signaux audites. Il s'agit d'un indice composite de surveillance,
pas d'une probabilite de defaut, d'une mesure pure de choc de marche ou d'un conseil
d'investissement. Les donnees annuelles et trimestrielles restent des facteurs lents.

La version precedente incluait 9 signaux ETF : TLT, HYG, LQD, SHY, SPY,
HYG/LQD, TLT/SHY, SPY/TLT et volatilite realisee HYG sur 30 seances.
Ils sont retires de la collecte, de la couverture, du score, des graphiques et de
l'export publics. Aucun prix de remplacement n'est fabrique. Les taux et spreads
FRED gardent leur definition initiale ; ils ne sont pas des prix d'ETF equivalents.

Le poids de base `market_prices` etait 0,04 sur 0,90 courant (4,44 %).
Son retrait porte le denominateur a 0,86. Les autres coefficients restent inchanges.
Cette renormalisation est un changement de methode permanent, jamais une reponse
dynamique a une panne. Un ecart a la bascule ne prouve pas une evolution du risque.
Les historiques doivent conserver la version originale et marquer la rupture,
sans recalcul ni raccordement retroactif silencieux.

## Familles et poids

| Famille | Signaux requis | Coefficient | Poids courant effectif |
| --- | ---: | ---: | ---: |
| `fiscal` | 4 | 0,22 | 25,5814 % |
| `rates_market` | 7 | 0,18 | 20,9302 % |
| `private_leverage` | 4 | 0,12 | 13,9535 % |
| `liquidity` | 4 | 0,10 | 11,6279 % |
| `treasury_daily` | 3 | 0,10 | 11,6279 % |
| `world_bank` | 4 | 0,04 | 4,6512 % |
| `global_credit` | 5 | 0,10 | 11,6279 % |
| `cbo_projection` (separe) | 4 | 0,10 historique | 0 % |

Les poids exacts sont les fractions coefficient/0,86, non les valeurs arrondies du
tableau. Le JSON publie ces fractions sous `methodology.current_bucket_weights`.

Sources et identifiants :

- FRED fiscal : `GFDEGDQ188S`, `FYGFGDQ188S`, `FYFSGDA188S`, `A091RC1Q027SBEA`.
- FRED taux/credit : `DGS2`, `DGS10`, `DGS30`, `T10Y2Y`, `T10YIE`, `BAMLC0A0CM`, `BAMLH0A0HYM2`.
- FRED dette privee : `TDSP`, `NCBCMDPMVCE`, `DRCCLACBS`, `DRBLACBS`.
- FRED liquidite : `NFCI`, `STLFSI4`, `WRESBAL`, `RRPONTSYD`.
- Treasury Debt to the Penny : stock total, part detenue par le public, croissance annualisee sur 90 jours.
- World Bank, USA uniquement : `GC.DOD.TOTL.GD.ZS`, `GC.XPN.INTP.RV.ZS`, `NY.GDP.MKTP.KD.ZG`, `FP.CPI.TOTL.ZG`.
- BIS, USA uniquement : credit-to-GDP gap, ratio credit/PIB, ratios de service de dette privee, des menages et des entreprises.
- CBO : dette detenue par le public/PIB, dette brute/PIB, deficit/PIB et interets nets/PIB, millesime de fevrier 2026 epingle dans `catalog.py`.

## Calcul

Pour les series standard, le dernier niveau est compare a la moyenne et a l'ecart-type
echantillonnal (`ddof=1`) de la fenetre incluant cette observation. La fenetre est de
5 ans pour FRED et les deux niveaux Treasury, 10 ans pour World Bank et les ratios BIS.
Il faut au moins 8 observations valides au total, 6 dans la fenetre et une variance
non nulle. Les doublons de date rendent le calcul indisponible.

```text
z = (dernier_niveau - moyenne_fenetre) / ecart_type_fenetre
signed_z = z si direction=up, -z si direction=down
score_signal = clip(50 + 15 * signed_z, 0, 100)
score_famille = somme(score_signal * poids_signal) / somme(poids_signal)
score_courant = somme(score_famille * coefficient_famille) / 0,86
```

Les poids individuels et directions sont dans `catalog.py` pour FRED, World Bank
et CBO, dans les fonctions de transformation de `data.py` pour Treasury et BIS.
Une famille doit etre complete pour produire son score.

Exceptions explicites, inchangees par cette migration :

- BIS credit gap : `clip(50 + 3 * gap_en_points_de_PIB, 0, 100)`, poids individuel 1,20 ; autres signaux BIS 0,90.
- Treasury : croissance = `(dette / reference_90j - 1) * 400`, reference au dernier jour disponible au plus tard 90 jours avant. C'est une extrapolation simple, pas un taux annuel observe. Score = `clip(50 + 3 * max(croissance - 5, -5), 0, 100)`. Poids 0,70 ; stock 1,00 et part publique 0,80.
- CBO : z-score sur les 30 dernieres annees de la trajectoire projetee, pas sur une serie de realisations. Le score retient aussi un plancher lineaire de niveau pour dette publique/PIB (50 a 80 %, 80 a 150 %), interets/PIB (50 a 2 %, 80 a 6 %) et deficit/PIB (50 a -3 %, 80 a -8 %). Ce bloc est exclu du score courant.

Seuils d'affichage : moins de 50 `Calm`, de 50 a moins de 65 `Elevated`,
de 65 a moins de 80 `Watch`, au moins 80 `Stress`. Ces seuils sont des conventions
du radar, pas des seuils officiels de crise ou des probabilites calibrees.

## Disponibilite et dates

Le score courant vaut `null` si l'un des 31 signaux requis est absent, invalide,
trop ancien ou sans historique exploitable. Aucun zero, score 50 ou poids de
remplacement. La couverture est ponderee par famille, pas simplement 31/31.
Un CBO absent degrade l'audit global mais ne suspend pas le score courant complet.

| Frequence / convention de date | Age maximal admis |
| --- | ---: |
| Quotidienne | 10 jours calendaires |
| Hebdomadaire | 28 jours |
| Trimestrielle FRED, debut de periode | 280 jours |
| Trimestrielle BIS, fin de periode | 300 jours |
| Annuelle World Bank, fin de periode | 900 jours |
| Annuelle FRED, debut de periode | 1 100 jours |

Ces tolerances ne garantissent pas la derniere publication. Les dates des observations
ne sont pas des dates de collecte. L'horizon CBO 2056 n'est pas une date de fraicheur.
La politique de fraicheur 3 separe deux delais reseau, comptes depuis la collecte
initiale de chaque reponse validee :

| Source | Cadence de renouvellement | Reutilisation maximale |
| --- | ---: | ---: |
| FRED et Treasury | 6 h | 48 h |
| Metadonnees trimestrielles FRED | 6 h | 48 h |
| BIS et World Bank | 24 h | 7 jours |
| Millesime CBO fige | 24 h | 30 jours |

Apres la cadence normale, la qualite globale porte `cached` et liste les signaux
concernes. La reponse reste celle de sa collecte initiale : aucune date n'est
rajeunie. Une publication plus recente peut ne pas encore avoir ete integree.
La premiere limite atteinte, age economique ou reutilisation reseau, exclut le signal.
Ce sont des tolerances de surveillance explicites, pas des garanties fournisseur.
La formule, les poids et la couverture obligatoire 31/31 restent inchanges.

Le diagnostic du 10 octobre a identifie deux echecs reseau World Bank consecutifs :
le premier conservait le cache, le second le rejetait a son ancien seuil de 24 h.
Cette politique remplace cette confusion entre cadence et expiration. Elle ne garantit
pas une disponibilite absolue si la panne depasse les limites publiees.

### Publications trimestrielles differees

Le 9 octobre 2026, `GFDEGDQ188S` et `FYGFGDQ188S` ont franchi la limite de
280 jours depuis le debut du T1 2026. FRED affichait toujours ce trimestre,
mis a jour le 25 juin. Il ne s'agissait ni d'un quota ni d'un cache expire.
La verification ponctuelle de disponibilite faite la veille ne couvrait pas
ce changement de jour. Les tests reproduisent desormais cette transition.

Les dates ne sont pas redatees et le seuil general de 280 jours reste en place.
Une serie FRED trimestrielle dispose d'une exception explicite `official_delayed`
si une reponse recente de `fred/series` confirme son identifiant, sa frequence
et exactement la derniere periode des observations collectees. La date FRED
`last_updated` est la mise a jour de la serie, pas la date du trimestre et pas
necessairement celle de sa premiere publication.

Toutes ces conditions sont requises :
- Observations et metadonnees collectees depuis moins de 48 heures ; au-dela de
  six heures, leur reutilisation porte aussi la mention de cache. Lire le cache
  ne modifie jamais ces dates.
- Mise a jour FRED non future, posterieure a la fin du trimestre et anterieure
  aux deux collectes. Aucun trimestre plus recent annonce dans les metadonnees.
- Au plus 120 jours calendaires depuis cette mise a jour (environ un cycle
  trimestriel et quatre semaines de marge).
- Au plus six mois et 30 jours depuis la fin du trimestre. Une simple revision
  de la serie ne prolonge donc pas indefiniment une observation ancienne.

Les deux dernieres bornes sont des choix de surveillance, pas des delais garantis
par FRED. Au-dela, ou sans confirmation, le signal reste exclu. L'exception ne
s'applique ni aux observations quotidiennes, ni aux valeurs invalides, ni aux
historiques insuffisants. La formule, les poids et les 31 signaux restent ceux
de la methode 2.0 ; `quality.policy_version` versionne cette regle separement.

La publication differee est visible dans le dashboard et dans les consommateurs
l0g. Le JSON fournit les horodatages, les bornes et les signaux qui approchent
de leur limite d'age sous quatorze jours. `valid_until` ne depasse pas la premiere
echeance d'eligibilite d'un signal courant. Les metadonnees sont demandees seulement
a l'approche du seuil (14 jours), avec renouvellement 6 h, anticipation normale du cache
et limitations fournisseur inchangees. Aucun appel n'est lance par le navigateur.

Sources du diagnostic et du contrat :
- [Ratio dette totale/PIB, definition et periode publiee](https://fred.stlouisfed.org/series/GFDEGDQ188S)
- [Ratio dette detenue par le public/PIB](https://fred.stlouisfed.org/series/FYGFGDQ188S)
- [Metadonnees FRED : observation_end, frequency_short, last_updated](https://fred.stlouisfed.org/docs/api/fred/series.html)
- [Calendrier officiel de la publication](https://fred.stlouisfed.org/releases/calendar?rid=263&y=2026)

## Acces, droits et provenance

La collecte configuree ne requiert pas d'abonnement payant ; FRED necessite une cle.
L'acces a une API ne vaut pas autorisation generale de redistribution. Les series
FRED detenues par des tiers conservent leurs restrictions, dont les indices ICE BofA.
L'autorisation commerciale de ces series n'est pas etablie par cette modification.
La licence MIT porte sur le code, pas sur les donnees.

- [FRED API et droits des series tierces](https://fred.stlouisfed.org/docs/api/terms_of_use.html)
- [Treasury Fiscal Data, documentation](https://fiscaldata.treasury.gov/api-documentation/)
- [World Bank Indicators API](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392-about-the-indicators-api-documentation)
- [BIS Data Portal](https://data.bis.org/)
- [CBO Open Data](https://github.com/US-CBO/cbo-data)

Le produit n'est pas certifie par ces fournisseurs. Les anciennes fonctions Massive
restent testees pour compatibilite technique et retour arriere ; elles ne sont pas
utilisees par l'application publique ou l'exporteur de methode 2.0.
