# État du projet

État éditorial au **14 septembre 2026**. Le projet est en développement.

## Sources disponibles

Broker avec missions, runbooks, permissions et audit ; mémoire SQLite/Qdrant ; orchestrateur avec budgets ; connecteur natif et pilote CLI ; application Ops et Atlas ; pont Zulip et PWA Web Push ; outils de sauvegarde, restauration et continuité.

## Validation historique

Sur l’installation de développement, des restaurations isolées des données applicatives ont été réalisées. La reprise des services et la lecture de la mémoire après un redémarrage réel ont été vérifiées le 14 septembre 2026. La réception d’une notification PWA sur iPhone a été confirmée. Un premier déploiement réel de la PWA a été validé ; une correction des permissions a ensuite été vérifiée sur les réponses HTTP intégrales. Les preuves opérationnelles privées ne sont pas publiées. Ces observations ne garantissent pas une installation sur une autre machine.

## Travaux restants

1. Finaliser et tester la coordination entre équipes d’agents.
2. Généraliser et recetter le circuit staging → production pour les autres applications.
3. Démontrer une reconstruction complète sur machine vierge.
4. Poursuivre l’indexation API sous budget et mesurer sa qualité.
5. Compléter les scénarios de notifications et de reprise : téléphone verrouillé, session expirée et retour après interruption.
6. Généraliser la configuration pour une installation reproductible hors de l’environnement d’origine.

## Publication

L’historique public démarre avec une copie des sources du chantier, y compris les changements récents non commités. Les données d’installation ont été remplacées par des exemples. Historiques Git privés, secrets, états d’exécution, sauvegardes et rapports privés sont exclus.

Les anciens manifestes, empreintes et scripts de migration servent de références et de fixtures. Ils ne sont pas des signatures valides de cet export modifié. Les versions, prix et catalogues historiques nécessitent leur propre actualisation.

Les runbooks historiques de publication Zulip v2/v3 sont conservés en `.yaml.example`, hors du registre actif : leurs helpers ne figurent pas dans la politique publique d’exécution.

## Supervision — livraison 0.8.3

Interface Infrastructure native avec graphiques, disques, processus, historique, illustrations embarquées et journaux. Collecte continue locale vérifiée ; extensions distantes préparées et soumises à approbation, sans les déclarer actives par anticipation. Pagination durable des archives et état des sources intégrés. La recette Tauri réelle et 174 tests natifs passent sur l’installation de développement. Les pilotes Docker autres que json-file et la récupération de journaux déjà supprimés ne sont pas couverts.
