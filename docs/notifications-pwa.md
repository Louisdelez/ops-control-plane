# Notifications de la PWA Autorisations

Choix de Louis : notifications directement dans la PWA sur iPhone, sans e-mail et sans inscription au relais push Zulip.

Dans Safari, ouvrir https://autorisations.example.org, puis Partager → Sur l’écran d’accueil. Ouvrir l’application depuis son icône et se connecter. Dans le menu latéral, choisir « Activer les notifications », accepter la demande iOS puis « Tester une notification ».

Sur iPhone, Web Push nécessite iOS 16.4 ou ultérieur et une application ajoutée à l’écran d’accueil. La demande de permission vient d’un geste explicite. Aucun bouton d’installation n’a été réintroduit dans le feed.

Les nouvelles autorisations encore en attente produisent une alerte générique chiffrée : « Une demande attend ta décision ». La notification ouvre le feed ; elle n’exécute aucune approbation. Les anciennes demandes ne déclenchent pas une rafale lors de l’activation. Chaque appareil a son abonnement, révocable dans le menu. La déconnexion supprime les abonnements liés à la session ; après expiration de session, se reconnecter pour reprendre les notifications.

Le serveur utilise Web Push/VAPID avec un sujet HTTPS, sans adresse e-mail. Abonnements chiffrés dans la base privée de la PWA, clé de signature persistante privée, endpoints limités aux services push connus, redirections HTTP refusées. Livraison dédupliquée et retries bornés ; les expirations 404/410 désactivent l’abonnement. L’acceptation HTTP par le relais ne prouve pas la réception physique sur iPhone.

Preuves : artifacts/finalisation-suite-2026-09-13/push-tests.log et ui.json. Le test physique de réception reste distinct de la recette Safari déjà confirmée.

Sources : https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/ ; https://github.com/web-push-libs/pywebpush.
