---
name: deploy-release
description: Prepare un deploiement de release avec artefacts verifies, approbation, controles post-deploiement et rollback explicite.
---

# Deploiement de release

Ce skill prepare et coordonne une release ; il ne donne pas le droit de la
deployer. Le catalogue Ops V1 fournit maintenant les contrats deterministes
`ops.release_plan.v1`, `ops.deploy_release.v1` et `ops.rollback_release.v1`.
Ils ne deviennent executables par Hermes qu'apres leur exposition par une
identite RBAC autorisee ; leur presence dans le depot n'accorde aucun droit.

Exige avant toute demande : projet, version actuelle et cible, provenance de
l'artefact, checksum ou signature, prerequis, sauvegarde attendue, fenetre,
tests de sante et rollback vers une version precise. Une verification absente
reste `inconnue`, jamais `OK`.

Si les outils sont disponibles, cree une mission idempotente puis consulte
`list_authorized_runbooks`. Utilise seulement un runbook retourne. La demande
`shared.change-request.v1`, lorsqu'elle est autorisee, enregistre une classe C :
elle ne deploie rien. Attends l'approbation humaine durable et un futur runbook
deterministe de deploiement avant toute mutation.

Le plan doit etre recalcule sous verrou et son `plan_hash` doit correspondre a
une approbation de classe C durable. L'archive ne contient jamais les chemins
persistants ; ceux-ci restent dans la racine partagee allowlistee. Aucun
deploiement n'est reussi avant les health checks declares. Le rollback
automatique n'est permis que s'il est preautorise dans la cible.

Ne contourne pas les budgets du broker. Apres execution par un systeme
autorise, exige les controles de sante definis et declenche le rollback prevu
si le critere d'arret est atteint. Sans outil, runbook ou preuve, transmets le
dossier a l'humain ; n'invente ni outil, ni commande, ni resultat.
