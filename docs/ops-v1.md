# Socle operationnel deterministe V1

Ce lot couvre le plan de controle des exigences operationnelles du cahier des
charges. Il n'ajoute volontairement aucun acces SSH, token fournisseur ou
secret. La configuration livree est donc utilisable pour validation locale,
mais toutes les cibles distantes et toutes les mutations restent fermees.

## Etat livre

- `scripts/ops-v1` : CLI Python standard-library, sans shell libre ni commande
  distante, avec sorties JSON bornees.
- `runbooks/contracts/catalog.json` : 10 contrats complets (permissions,
  preconditions, resultat attendu, erreurs, timeout, tentative, risque,
  idempotence, dry-run et rollback).
- `inventory/ops-v1.json` : inventaire structure de cinq ressources issu des
  documents Master. Les alias sont conserves, jamais les cles. Les cinq sondes
  DNS/TLS/HTTP/TCP/backup sont desactivees.
- `inventory/release-targets.json` et `inventory/config-targets.json` : aucun
  target actif par defaut. Une operation non enregistree echoue.
- release generique : checksum SHA-256, identite version/commit/build, archive
  tar sure, staging immuable, preuve de backup stateful, persistance hors
  artefact, lock par ressource, commutation atomique, services allowlistes,
  health checks et rollback preautorise.
- checks en lecture seule : DNS, TLS/chaine/expiration, HTTP borne sans
  redirection automatique, TCP, preuve de backup, fichier, service et journal.
- comparaison d'observation : signale absence, etat divergent et element
  inattendu sans jamais modifier l'inventaire.
- file bornee : classification deterministe critique/haute/normale/basse,
  FIFO dans une priorite, missions bloquees apres les missions executables et
  une seule action concurrente sur ce Dell 8 Go.
- config diff : cible et candidat allowlistes, hash avant/apres, validation
  texte/JSON et masquage des affectations sensibles. Il n'existe pas de mode
  apply dans cet outil de classe A.
- retention : produit un plan hashable et ne supprime rien. Les politiques
  livrees sont desactivees et orientent les fichiers vers une quarantaine
  recuperable.
- skills : registre de versions, validation de structure/tests/permissions et
  promotion seulement `manual -> semi-automatic -> automatic`. La promotion
  cree un fichier candidat distinct et exige tests, inspection, permissions,
  approbation, commit Git, trois puis dix executions sans echec.
- audit de release : evenements JSONL lies par SHA-256, phases explicites
  `observation`, `analysis`, `action`, approbation et rollback.
- reporting : phrase courte pour Zulip, rapport JSON detaille et metriques
  Prometheus. Les alertes detectent echec ou obsolescence des checks actifs.

## Contrat de release

Une cible est une decision d'exploitation, pas une donnee devinee. Elle doit
declarer exactement : projet, ressource, environnement, racines incoming,
releases et persistance, lien `current`, chemins persistants, services,
health checks, politique de backup, rollback, approbateurs, audit et locks.

Une cible stateful active sans preuve de backup est rejetee. Une cible active
sans health check ou approbateur est rejetee. L'archive ne peut contenir ni
symlink, hardlink, device, FIFO, traversal, metadata reservee, ni fichier sous
un chemin persistant. Les migrations generiques sont refusees : une migration
necessite un runbook specialise, teste et approuve.

`release-plan` ne modifie rien et lie dans `plan_hash` : le manifeste complet,
le target, le checksum reel, la release courante, l'inventaire de l'archive et
le snapshot de backup. Un changement pendant l'attente d'approbation invalide
donc le plan.

`release-apply` exige simultanement :

1. `--execute` ;
2. un target `enabled=true` ;
3. une approbation de classe C nommee `<plan_hash>.json` dans le repertoire de
   confiance ;
4. un approbateur allowliste et une expiration inferieure a 24 heures ;
5. pour la production, execution root et fichier/repertoire d'approbation
   root-only ;
6. le meme plan recalcule apres acquisition du verrou de ressource.

L'executor stage sans ecraser une release existante, arrete/demarre chaque
unite allowlistee au plus une fois, commute `current` avec `os.replace`, puis
execute tous les checks. Une erreur est journalisee et stoppe le workflow. Le
rollback n'est automatique que si `rollback_automatic=true` faisait partie du
plan approuve ; sinon le resultat indique `approval-required`.

## Observation et inventaire

Les observations distantes doivent etre produites par un collecteur separe,
autorise et en lecture seule. `discovery-compare` accepte un document JSON
borne et recent. Il ne possede aucune instruction pour supprimer, adopter ou
modifier une ressource. Un service inattendu devient une anomalie `info`, pas
une nouvelle verite.

Commandes de validation sans reseau :

```bash
python3 scripts/ops-v1 contracts-validate --catalog runbooks/contracts/catalog.json
python3 scripts/ops-v1 inventory-validate --inventory inventory/ops-v1.json
python3 scripts/ops-v1 check --inventory inventory/ops-v1.json
python3 scripts/ops-v1 retention-plan --policy monitoring/ops-v1-retention.json
python3 scripts/ops-v1 skills-validate \
  --registry config/hermes/skill-registry.json \
  --skills-root config/hermes/skills \
  --repository-root .
```

Sans `--probe`, une sonde active est `skipped/probe_not_requested`. Avec
`--probe`, une sonde `enabled=false` reste `skipped/disabled`. Ainsi une simple
installation ne contacte pas les domaines historiques.

## Installation et activation

L'installation preserve toute configuration operateur deja presente :

```bash
sudo scripts/install-ops-v1.sh
```

Elle installe le CLI, les contrats, les registres et les units mais desactive
les timers. Apres revue de `/etc/ops-v1/inventory.json`, l'activation des seules
observations read-only et du rapport se fait explicitement :

```bash
sudo scripts/install-ops-v1.sh --enable-observation
```

Le remplacement d'un registre local divergent exige
`--replace-managed-configs`. Ce flag ne cree toutefois aucun target : les
registres source sont vides.

## Monitoring, alertes et retention

`ops-v1-observe.timer` execute toutes les cinq minutes les sondes activees.
`ops-v1-report.timer` produit chaque jour `latest.txt`, `latest.json` et une
copie datee. `ops-local-metrics` expose :

- `ops_v1_enabled_checks` ;
- `ops_v1_observation_enabled` et `ops_v1_check_result_valid` ;
- `ops_v1_check_failures` ;
- `ops_v1_check_result_timestamp_seconds`.

Prometheus declenche `DellOpsV1CheckFailure` sans LLM et
`DellOpsV1ChecksStale` si une sonde active ne produit rien depuis 15 minutes.
Le rapport principal `/var/lib/ops-reports/daily/latest.json`, deja consomme par
le bridge Zulip, reprend aussi le nombre de checks, leurs echecs et leur age. Il
classe séparément les backups broker, mémoire, test de restauration mémoire et
Raft OpenBao en `absent`, `failed`, `stale` ou `ok`. Si aucune sonde distante
n'est active, il le dit explicitement. Le LLM ne fait qu'interpreter ces faits.

La retention livree est desactivee. Son mode actuel est volontairement
`plan -> quarantaine recuperable`; aucune suppression directe n'existe. Les
logs systeme restent geres par journald/logrotate et les evenements importants
sont resumes dans la memoire par le lot memoire, pas recopies integralement.

## Activation d'une vraie cible

Avant de passer un champ `enabled` a `true`, il faut une revue Git incluant :

- identite technique OS/SSH et policy OpenBao minimale ;
- cible exacte et proprietaire projet/shared ;
- collecteur read-only teste sans secret en sortie ;
- backup recent et controle de restauration pour tout etat persistant ;
- health checks couvrant service, port/endpoint, logs et dependances ;
- artefact reproductible signe ou au minimum SHA-256 ;
- approbateurs Zulip verifies ;
- rollback teste en staging ;
- droits filesystem/sudoers limites aux seuls helpers ;
- exposition du contrat via le broker avec classe C.

Tant que ces elements ne sont pas presents, `disabled` est l'etat correct et
la sortie humaine doit dire « etat distant inconnu », jamais « tout est OK ».

## Limites explicites

Ce lot ne rend pas une mutation distante disponible dans Hermes : il fournit
les contrats et l'executor local a brancher derriere le broker. Il ne configure
pas OVH, Infomaniak, Nginx, Traefik, WireGuard, Minecraft, PostgreSQL ou Restic
sur une machine distante. Ajouter silencieusement ces acces aurait contourne
OpenBao, RBAC, les approbations et la contrainte fail-closed.

Les tests couvrent notamment l'absence de probe quand disabled, le scope d'une
preuve de backup, la derive sans mutation, le traversal tar, la protection de
la persistance, la commutation atomique, le rollback sur health check, le diff
redige, les locks, la promotion de skill et le rapport.
