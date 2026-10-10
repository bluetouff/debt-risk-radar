# Export public latest.json

Endpoint : [https://debt.l0g.fr/latest.json](https://debt.l0g.fr/latest.json).
Fichier statique HTTPS servi par Apache, sans cle publique ni parametre de collecte.
Le timer systemd le regenere toutes les 15 minutes par defaut. Consommer ce fichier
ne declenche aucun appel aux fournisseurs.

## Versions

- `schema_version = "1.2"` : structure JSON, extensions additives a la version 1.1.
- `methodology.id = "us-debt-institutional"`, `methodology.version = "2.0"` : definition economique du score.
- Un schema compatible ne rend pas les valeurs comparables : les 9 signaux ETF de
  l'ancienne methode sont retires. `methodology.comparable_with_previous_method = false`.
- Un snapshot anterieur sans `methodology` reste une observation de methode ancienne
  non versionnee. Ne jamais lui attribuer retroactivement la version 2.0.

## Champs

| Champ | Signification |
| --- | --- |
| `generated_at` | Date UTC de generation du fichier, pas date des observations |
| `source_sha` | SHA Git complet du marqueur de release verifie a l'activation ; `null` sans marqueur valide, notamment en developpement |
| `valid_until` | Limite UTC de validite du snapshot, generation + deux intervalles de rafraichissement |
| `methodology` | Identifiant, version, description, poids courants normalises, buckets retires et rupture de comparabilite |
| `score.current_stress` | Nombre 0-100 si les 31 signaux courants sont eligibles, sinon `null` |
| `score.status` | `Calm`, `Elevated`, `Watch`, `Stress` ou `No data` |
| `score.expected_signals`, `score.eligible_signals` | Compteurs courants, respectivement 31 et 0-31 |
| `score.coverage` | Somme des poids normalises des familles multiplies par leur fraction de signaux eligibles |
| `score.excluded_buckets` | `cbo_projection`, conserve mais structurel |
| `score.buckets` | Huit familles auditees, dont une structurelle |
| `score.structural` | Score CBO separe, sans contribution au score courant |
| `signals` | Les 35 signaux attendus, y compris ceux indisponibles |
| `top_signals` | Au plus 20 signaux courants eligibles classes par risque, configurable cote serveur |
| `quality` | Audit global des 35 signaux ; `ok`, `official-delayed` ou `degraded`, compteurs et identifiants indisponibles |
| `sources` | Effectifs, dates des observations et horizon des projections par source |
| `issues` | Source et diagnostic controle, sans secret ni reponse brute |
| `collection` | Diagnostic de renouvellement des cinq fournisseurs actifs |
| `refresh` | Cadence d'export et TTL sources ; `market` = 21 600 s pour FRED/Treasury, `institutional` = 86 400 s pour les autres, cles conservees pour compatibilite |

Chaque famille expose `weight` (coefficient historique de base) et `current_weight`
(fraction normalisee du score courant, zero pour CBO), `metrics`, `expected_metrics`,
`coverage`, `score_role`, `score` et `included_in_overall`.
`current_weight` decrit la methode meme si le score est suspendu ; `included_in_overall`
est alors faux. Ne pas interpreter le coefficient CBO historique 0,10 comme une
contribution de 10 % au score courant.

Chaque signal contient notamment `bucket`, `series_id`, `name`, `source`, `unit`,
`date`, `current`, `signed_z`, `risk_score`, `quality`, `quality_detail`, `eligible`,
`frequency`, `observation_age_days` et `max_age_days`.

La politique de fraicheur `quality.policy_version = "3"` conserve le schema `1.2`
et la formule economique `2.0`. Un signal trimestriel `quality = "official_delayed"`
est eligible seulement avec confirmation FRED recente et respect des bornes
decrites dans `METHODOLOGY.md`. Ce n'est pas une observation du jour.
`quality.delayed_signals` liste ces signaux, et `quality.expiring_signals` annonce
leurs prochaines limites d'age (14 jours). Une publication differee ne doit etre
presentee ni comme une panne de collecte ni comme une qualite nominale.

`quality.status = "cached"` signale au moins une reponse reutilisee au-dela de sa
cadence de renouvellement, mais dans sa limite maximale et son age economique admis.
`quality.cached_signals` donne leurs identifiants. Le score exige toujours 31/31.
`quality.cache_expiring_signals` liste `series_id` et `expires_at` pour les reponses
reutilisees dont l'echeance effective arrive sous 24 heures, pour alerte avant rupture.
Si une publication differee coexiste, `delayed_signals` reste renseigne ; une
ineligibilite ou un flux absent donne priorite a `degraded`.
Les consommateurs doivent afficher le recours au cache, pas une qualite nominale.
Chaque signal expose `cache_status` (`fresh`, `cached`, `expired`, `invalid`, `unknown`),
`cache_expires_at`, `cache_refresh_seconds`, `cache_max_age_seconds` et la collecte
originale `observation_checked_at`. `refresh.source_ttl_seconds` reste la cadence ;
`refresh.source_max_cache_age_seconds` donne les maxima. Voir le tableau de la methode.

Les champs additionnels des signaux sont `freshness_basis`, `freshness_limit_at`
(limite d'age), `freshness_expires_at` (echeance effective incluant les caches),
`observation_checked_at`, `publication_observation_end`, `publication_updated_at`,
`publication_checked_at` et `publication_frequency`. Les champs de date sont
des horodatages UTC, sinon `null` ; la frequence est `Q` et la base de fraicheur
est un identifiant de politique.
`valid_until` est borne par la premiere echeance effective courante, y compris
si elle tombe entre deux passages du timer. L'heure de lecture du cache n'est
jamais une nouvelle heure de collecte.
La generation conserve ses fractions de seconde : un arrondi vers le bas ne
doit pas transformer une confirmation deja recue en horodatage futur. Les dates
reellement futures restent refusees, sans marge ajoutee a ce controle.
Les valeurs non finies sont serialisees en `null`, jamais `NaN` ou `Infinity`.
Un `signed_z` nul peut etre normal pour un calcul fonde sur le niveau, tel le credit gap.
La date CBO est un horizon futur et sa qualite vaut `projection`, pas une observation courante.

## Regles consommateurs

1. Verifier HTTP, Content-Type JSON et parsing strict. Conserver la provenance HTTPS.
2. Verifier une version de schema et une methode connues ; ne pas deviner une version absente.
3. Refuser un `generated_at` futur ou un `valid_until` expire/invalide. Ne pas redater un ancien snapshot.
4. Accepter un score seulement s'il est numerique, fini, dans 0-100, avec couverture complete
   et 31 signaux courants eligibles. Un `null` signifie indisponible, jamais risque nul.
5. Lire `quality`, `issues` et `collection` separement. Un CBO absent peut donner une qualite
   degradee alors que le score courant reste valide. Une pause peut coexister avec un cache valide.
6. Stocker la methode avec chaque observation historique et interrompre les comparaisons
   entre methodes. Ne pas reprendre une valeur de l'ancienne methode comme repli de la nouvelle.

`collection.status` vaut `ok`, `paused` ou `unknown`. `providers` contient seulement
les pauses des fournisseurs actifs, avec `source`, `reason`, `last_attempt_at`,
`retry_at` en UTC. Les raisons sont `rate_limit`, `authorization`, `http_error`,
`network_error`, `invalid_response` ou `upstream_failure`.
L'ancienne pause Massive reste en cache prive pour le rollback, mais ne decrit plus
la collecte active et n'est donc plus exportee. `ok` ne certifie pas des observations fraiches.

## Exploitation

Le fichier est remplace atomiquement. Le service d'export sort avec le code 2 si le
score courant est indisponible, tout en publiant le diagnostic JSON degrade ; code 1
si l'ecriture echoue. Une erreur d'ecriture peut laisser l'ancien fichier sur disque :
le consommateur doit toujours respecter `valid_until`.

Le producteur n'archive pas les anciennes valeurs. La preservation des versions et
la rupture de serie sont a implementer dans chaque consommateur, notamment les cartes
et historiques l0g, avant de presenter une comparaison temporelle apres la migration.
Voir [DEPLOYMENT.md](DEPLOYMENT.md) pour la verification et le retour arriere.
Les droits sur les donnees restent ceux des sources : [METHODOLOGY.md](METHODOLOGY.md).
