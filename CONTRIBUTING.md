# Contribuer

Les contributions au code original sont proposées sous licence MIT. Préserver les licences tierces.

1. Décrire le problème dans une issue, sans données privées.
2. Créer une branche et limiter la modification au problème traité.
3. Suivre le [guide de développement](docs/getting-started.md) et tester le composant concerné.
4. Expliquer dans la pull request le comportement avant/après, les validations et les limites.

Ne pas joindre de clé API, cookie, base ou journal brut. Utiliser des données synthétiques. Séparer tests unitaires et opérations réseau ou privilégiées.

Pour le broker, vérifier refus d’accès, idempotence, interruptions et audit. Pour la mémoire, vérifier périmètres et conservation en mode dégradé. Pour les interfaces, vérifier origines et frontières IPC. Une simulation n’est pas une validation en production.
