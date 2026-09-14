# Validation de la publication

Contrôles effectués le 14 septembre 2026 sur la copie publique, avec les environnements Python de développement déjà présents sur Linux.

| Suite | Résultat |
| --- | --- |
| Broker | 98 réussis |
| Mémoire | 103 réussis |
| Orchestrateur | 301 réussis, 1 ignoré |
| Intégration native | 117 réussis |
| Pont Zulip | 73 réussis |
| Découverte fournisseurs | 45 réussis |
| PWA Autorisations | 26 réussis |

Total : **763 réussis, 1 ignoré**. Le test ignoré dépend d’un catalogue utilisateur privé, absent de la publication. Les tests PWA ont été exécutés avec les dépendances Web Push de l’environnement PWA et pytest de l’environnement de développement.

Le chargement du registre du broker a révélé deux anciens runbooks de publication dont les helpers ne sont pas autorisés par la politique fournie. Ils ont été conservés sous extension `.yaml.example`, hors registre actif ; aucun droit supplémentaire n’a été ajouté.

Les fichiers Python, JSON et TOML ont été analysés syntaxiquement. Les liens des nouveaux guides ont été vérifiés. Le `.gitignore` a été testé sur des chemins sensibles synthétiques. Le contrôle de publication utilise Gitleaks 8.30.1, dont l’archive a été vérifiée contre les sommes de contrôle de la release amont.

Ce passage ne comprend pas une nouvelle compilation Rust/Tauri, l’ensemble des anciennes suites de bootstrap, une installation depuis zéro, un déploiement distant ou un test physique de notification mobile. Les preuves d’exploitation privées et les environnements virtuels ne sont pas inclus.
