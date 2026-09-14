# Architecture

## Responsabilités

Le **broker** conserve les missions et actions. Il vérifie les rôles, projets et runbooks, applique les approbations requises et enregistre les résultats. Il expose une API locale et des outils MCP ; des helpers déterministes exécutent les opérations bornées.

La **mémoire** conserve le catalogue dans SQLite et les index reconstruisibles dans Qdrant. Chaque entrée porte source, date, classification et périmètre. Les index locaux et API restent séparés. La conservation locale doit continuer lorsque le fournisseur ou son budget est indisponible.

L’**orchestrateur** route les tâches selon les rôles, fournisseurs et budgets configurés. Il conserve les traces d’appels et alimente Atlas. Une estimation de coût ne constitue pas un solde fournisseur. Les prix et modèles du catalogue sont des données datées à actualiser.

Le **connecteur natif** relie Zulip, les clients CLI, Hermes et les services Ops. Sa base SQLite suit demandes, tentatives et livraisons. Une livraison ambiguë nécessite une revue avant rejeu. Les bases internes des produits restent distinctes.

L’**application Ops** utilise Rust/Tauri et des vues web natives. Atlas réutilise le moteur de `apps/model-manager`. Les pages des services distants ne doivent pas accéder aux commandes privilégiées de la barre locale ou des terminaux.

La **PWA Autorisations** sert aux approbations et aux abonnements Web Push. La présence du code serveur ne prouve pas la réception sur un téléphone particulier.

## Contrôle des actions

1. Une demande arrive avec une identité et un périmètre.
2. Le broker vérifie le runbook et les paramètres autorisés.
3. La politique détermine budget, approbation et conditions d’exécution.
4. Le helper exécute son contrat borné.
5. Résultat, preuves et passage de relais sont enregistrés.

La classe A concerne l’observation ; la classe B les opérations bornées par la politique ; la classe C les actions nécessitant l’approbation prévue. Un texte de modèle ne remplace pas ces contrôles.

**OpenBao** fournit les secrets aux identités de service. Les tokens, mots de passe, cookies et clés de récupération ne sont ni des souvenirs ni des fichiers du dépôt.

## Persistance

| État | Support |
| --- | --- |
| Missions et actions | Base du broker et audit |
| Souvenirs et versions | Catalogue SQLite de la mémoire |
| Recherche vectorielle | Qdrant, index reconstruisible |
| Demandes et réponses | Base du connecteur |
| Secrets | OpenBao et sauvegardes chiffrées |
| Zulip | PostgreSQL et données du produit |

Une reprise des services après redémarrage et une reconstruction sur machine vierge demandent des validations distinctes.
