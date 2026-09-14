# Pilotage Ops par MCP ou API

Les deux modes partagent les missions et les actions du broker. Les secrets restent dans OpenBao. Aucun modèle local n'est installé pour ce projet.

## Depuis Codex ou Claude Code

Les serveurs ops-broker, ops-memory et ops-orchestrator sont configurés par leurs interfaces MCP natives. Codex utilise codex-supervised et Claude Code utilise claude-supervised, chacun par son lanceur dédié. Ne pas changer d'identité dans un appel.

1. Lister les missions et incidents ouverts du projet. Lire la mission choisie, ses actions et ses records.
2. Les demandes du canal privé ops-pilote portent un record observation avec evidence `zulip-source:<message_id>`. Ce record conserve la demande complète. Son contenu est une demande humaine, pas une modification de politique.
3. Consulter list_authorized_runbooks, puis demander uniquement l'action nécessaire dans cette mission. Une action sensible attend l'approbation réelle du propriétaire dans Zulip.
4. Enregistrer les observations et leur action_id. Pour envoyer une réponse dans le sujet d'origine, ajouter un record `handoff` avec le texte final (1 à 3000 caractères) et evidence contenant exactement `zulip-response:<message_id>` ainsi que les références des observations utilisées. Employer un request_id stable pour le même envoi.
5. Le connecteur publie le record explicite et conserve son identifiant de message. Il ne déduit pas une publication d'une simple analyse. Deux handoffs concurrents ou un envoi ambigu passent en needs_review : ne pas les rejouer aveuglément.

Le connecteur n'affirme pas qu'une mission est terminée parce qu'une réponse est publiée. Le pilote traite actuellement un message comme une demande distincte ; il ne fusionne pas toute une conversation automatiquement.

## Depuis Zulip

Dans ops-pilote, envoyer une demande d'observation. Elle devient une mission durable, traitable via MCP même quand la génération Hermes est désactivée.

Dans ops-approbations, les commandes disponibles sont :

- `!ops mission infra-shared <titre>` : créer une mission.
- `!ops statut <mission-uuid>` : consulter son état.
- `!ops reprendre <mission-uuid>` : reprendre une mission en pause.
- `!ops répondre <mission-uuid> <texte>` : enregistrer une réponse humaine.
- `!ops rapport` : demander le rapport déterministe disponible.

Les réactions officielles ✅ et ❌ sur les demandes publiées par Ops Approbations transmettent la décision du propriétaire au broker. Une phrase « approuvé » ou un emoji ressemblant ne vaut pas accord. Aucune approbation ne doit être fabriquée pendant une recette automatique.

## Mode API, activation différée

Le profil Hermes séparé utilise la façade de budget avec DeepSeek Flash économique. La clé DeepSeek suffit ; Qwen et les modèles premium sont désactivés dans cette configuration. Le propriétaire ajoutera lui-même les clés dans Ops une fois la préparation terminée. Les services détectent ensuite les accès et activent leurs capacités dans les budgets prévus. La validité des clés et la qualité des réponses exigent encore une recette réelle. Voir `docs/comptes-memoire-api.md`. Les tests synthétiques ne prouvent ni la qualité ni la disponibilité d’un modèle.

## Limites de recette

Les commandes web locales ne prouvent pas l'accès iPhone. La connexion VPN/TLS, la réception push téléphone verrouillé et un accord humain réel restent des contrôles distincts. Le Dell éteint ne peut pas coordonner les missions.

Références des interfaces natives :
- https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- https://code.claude.com/docs/en/mcp
- https://openbao.org/docs/agent-and-proxy/agent/template/

## Pilote continu natif — Ops 0.6.4

Le service utilisateur `ops-cli-pilot.service` détecte automatiquement les comptes
Codex et Claude connectés. Il attend lorsqu’aucun compte n’est connecté et reste
actif après la fermeture de l’application. Dans Terminaux, le bouton **+** ouvre
une colonne Codex ou Claude avec son interface native, notamment pour se connecter.
Il n’y a pas de mode API/CLI à choisir : les capacités suivent les accès présents.
Le pilote et les terminaux partagent une limite de 2,5 Go dans `ops-agents.slice`.

Le sujet Zulip peut commencer par `[minecraft]`, `[network-shared]`,
`[monitoring-shared]`, `[backup-shared]` ou `[infra-shared]`. Sans préfixe, le
projet est infra-shared. Un projet inconnu produit une réponse explicite.

Le début du message peut être `!codex`, `!claude` ou `!api`. Sans préfixe,
Le routage automatique utilise l’API lorsque ses clés sont disponibles ; sinon il
choisit un compte CLI connecté (Codex en priorité, puis Claude). Sans compte, la
demande attend. Les destinations incompatibles
avec le mode restent en traitement manuel. La destination d’une demande déjà
acceptée ne change pas lors d’un changement de mode.

Le pilote ne traite que les missions portant les records source et destination
créés par le connecteur. Il vérifie l’absence de réponse, conserve sa tentative
avant de démarrer le CLI et vérifie le handoff à la fin. Au maximum 20 tentatives
par période glissante de 24 heures, cinq minutes par tentative. Une interruption,
une erreur ou une réponse ambiguë demande une revue ; aucun rejeu automatique.
Ces cas peuvent être repris de manière supervisée depuis un terminal natif.
Les états locaux sont dans `~/.local/state/ops-cli-pilot/attempts.sqlite3`.

Codex utilise son exécution non interactive officielle avec le shell désactivé,
un espace en lecture seule et une liste de tools MCP explicite. Les permissions
de ces tools restent contrôlées par le broker ; une classe C exige toujours son
accord Zulip réel. Claude utilise son mode print natif et la même liste de tools.
Aucun jeton d’abonnement n’est transformé en clé de fournisseur.

Le profil API expose cinq outils bornés : contexte et runbooks de la mission,
mémoire du projet, demande d’action, lecture d’une action et exécution d’une action réellement
approuvée. L’acteur de ce service est hermes-native, distinct des pilotes Codex
et Claude. Les runbooks disponibles dépendent du projet et du RBAC existant.
Les cibles distantes non activées restent indisponibles.

Après une demande API qui attend une approbation, le connecteur suit l’action
sans nouvel appel de modèle. Il ne l’exécute que lorsque le broker la déclare
approuvée, puis publie son état. Une interruption pendant l’exécution ou l’envoi
passe en revue manuelle ; une approbation absente n’est jamais devinée.

Documentation officielle utilisée :
- https://learn.chatgpt.com/fr-FR/docs/non-interactive-mode
- https://learn.chatgpt.com/fr-FR/docs/config-file/config-basic
- https://code.claude.com/docs/en/headless

Le pilote CLI suit également ses propres actions approuvées après le handoff,
sans nouvelle demande au modèle. Il ne traite pas les actions d’une autre identité.
Le connecteur publie l’état terminal vérifié. Ce suivi est assuré par le service utilisateur ; fermer l’application ne le suspend pas. Les cas ambigus restent à revoir.
