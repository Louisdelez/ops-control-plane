# Exploitation et récupération

## Missions

Lire la mission dans le broker, puis ses derniers records, checkpoints et actions. Comparer la prochaine étape à l’état réel. Un souvenir ou checkpoint ancien est historique, jamais une preuve actuelle ni une approbation.

Avant une action, vérifier identité, projet, environnement, runbook, paramètres, budget et approbation requise. Après une étape significative, enregistrer résultat et preuves. Le passage de relais précise objectif, vérifications, tâches restantes et prochaine action.

Après interruption, reprendre les identifiants durables. Ne pas créer une nouvelle tentative pour contourner une livraison ambiguë.

## Mémoire

Rechercher dans le périmètre autorisé et vérifier les faits auprès de leurs sources. Une correction référence l’entrée précédente sans effacer son historique. Conserver sources et dates, pas des conversations ou journaux bruts.

Exclure mots de passe, tokens, cookies et clés privées. Attendre la confirmation de stockage avant d’annoncer une sauvegarde. Si l’indexation API est autorisée, demander l’indexation de l’identifiant conservé. Un budget épuisé laisse l’index API en attente sans empêcher la conservation locale.

## Continuité

`native_ops/continuity_check.py` et `native_ops/memory_sync.py` fournissent des contrôles et une collecte périodique. Les rapports utilisateur sont, selon l’installation, sous `~/.local/state/ops-continuity/` et `~/.local/state/ops-memory-sync/`.

Vérifier date, démarrage courant, services et lisibilité de la mission. Tester séparément interfaces graphiques, services distants et notifications mobiles.

## Sauvegardes et restauration

Couvrir broker, connecteur, catalogue mémoire, Qdrant, Zulip, OpenBao et données de la PWA. Adapter les outils aux chemins, comptes et destinations propres au déploiement. Conserver sauvegardes chiffrées et clé de récupération séparément.

Tester d’abord les restaurations dans un environnement isolé : déchiffrement, import, intégrité et lecture autorisée. Une restauration applicative ne valide pas la reconstruction du système, du réseau, des comptes et des services sur une machine neuve.

## Notifications

La PWA fournit abonnement, désabonnement et test Web Push. L’utilisateur accorde la permission dans son navigateur. Valider la réception réelle sur le téléphone cible, selon le réseau et le verrouillage attendus. Voir la [référence Web Push](notifications-pwa.md).
