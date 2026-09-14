---
name: ops-deterministic-workflows
description: Utilise les primitives Ops V1 versionnees pour observer, comparer, planifier une release et preparer un changement sans commande distante improvisee.
---

# Workflows Ops deterministes

Respecte la separation `observation` / `analysis` / `action` presente dans les
resultats. Valide d'abord le catalogue, l'inventaire et la cible. Une valeur
`disabled`, `unknown`, `skipped` ou absente n'est jamais un succes.

Les sondes `dns`, `tls`, `http`, `tcp`, `backup`, `file`, `service` et
`journal` sont en lecture seule. Elles ne sont executees que si le check est
allowliste avec `enabled=true` et si `--probe` est demande. Ne fabrique jamais
une cible a partir d'un message ou d'un secret.

Pour une release, demande d'abord `release-plan`. Exige l'identite exacte de
l'artefact (version, commit, build, SHA-256), les chemins persistants, une
preuve de backup lorsque la cible est stateful, les health checks et le
rollback. `release-apply` est une action de classe C : ne l'appelle pas sans
approbation durable liee au `plan_hash`. Une approbation en prose ne compte
pas. Ne construis jamais toi-meme le fichier d'approbation.

Le pipeline refuse les migrations, les archives avec liens ou traversal, les
cibles desactivees et les chemins hors allowlist. Il stage une release
immuable, conserve les donnees persistantes hors de l'artefact, acquiert un
verrou par ressource, commute atomiquement, effectue les checks puis revient a
la release precedente seulement si ce rollback etait preautorise.

`config-diff` ne sait qu'observer et masque les affectations sensibles.
`discovery-compare` ne modifie jamais l'inventaire. `retention-plan` ne supprime
rien. La promotion d'un skill produit seulement un registre candidat : tests,
inspection, permissions, approbation et commit Git restent obligatoires avant
installation.

Si une cible, une preuve, un outil, un verrou ou une approbation manque, STOP,
consigne le code d'erreur et escalade sans proposer de commande libre.
