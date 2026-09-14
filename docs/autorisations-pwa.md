# Ops — Autorisations

Application publiée sur https://autorisations.example.org, intégrée à Ops 0.6.19
juste après Zulip. Le site example.org est conservé.

## Utilisation

Connexion avec l’adresse e-mail et le mot de passe du compte Zulip autorisé.
Le menu en haut à gauche donne accès aux demandes en attente, acceptées,
refusées, à traiter plus tard et expirées. Le message tient sur une ligne ;
« Voir plus » révèle le texte complet. Le feed se synchronise toutes les cinq
secondes lorsqu’il est visible. Le bouton de pagination conserve l’accès aux
anciens messages.

Accepter et refuser ajoutent respectivement les réactions Unicode officielles
✅ et ❌ avec l’identité réelle de l’utilisateur. Le bridge Zulip existant vérifie
ces réactions et le broker conserve la décision. L’application attend cette
confirmation ; une réaction seule n’est jamais présentée comme une approbation
validée. Les actions ne sont jamais exécutées par cette application.

Plus tard ajoute ⌛ sur la demande Zulip. Reprendre retire cette réaction. Cette
mise de côté est partagée entre appareils et n’allonge jamais l’échéance d’une
autorisation. Une demande expirée nécessite une nouvelle demande dans le circuit
existant. Les sujets historiques « ✔ approbations » sont visibles, mais aucune
nouvelle décision n’est permise sur ces sujets résolus.

Le navigateur peut proposer l’installation. Sur iOS : Safari → Partager → Sur
l’écran d’accueil. Sur Android/PC : Installer dans le menu du navigateur lorsque
cette fonctionnalité est disponible. L’application reste utilisable comme site
web.

## Architecture et limites de confiance

- Le service `ops-approvals` écoute uniquement sur 127.0.0.1:9128 sur le Dell,
  sous l’utilisateur dédié opsapprovals. Il n’a aucun accès au broker ni à
  OpenBao. Les comptes autorisés reprennent les identifiants humains explicitement
  admis par le bridge existant (actuellement utilisateur Zulip 8).
- `ops-approvals-export` lit les bases broker et publications en mode SQLite
  lecture seule. Il exporte uniquement les correspondances message/action,
  l’état, l’échéance, la date et la décision. Aucun paramètre de runbook,
  secret, sortie d’exécution ou jeton n’est exporté. Le service Web refuse les
  décisions si la projection date de plus de 15 secondes.
- Le texte des demandes vient de l’API Zulip du compte connecté, dans le canal
  exact. Il doit correspondre à un message publié, au bot attendu et au marqueur
  d’action attendu. Le texte est rendu avec textContent, jamais innerHTML.
- Le mot de passe sert seulement à la connexion officielle Zulip et n’est pas
  enregistré. La clé de session API obtenue reste chiffrée côté serveur dans
  /var/lib/ops-approvals/sessions.db, avec clé privée locale session.key. Le
  navigateur reçoit seulement un cookie opaque Secure, HttpOnly, SameSite Strict,
  préfixé __Host-. Durée maximale : 30 jours, révocable par déconnexion.
  L’état actif et les droits du compte Zulip sont revérifiés à chaque appel.
- Contrôle strict d’Origin pour les écritures, taille limitée à 8 Ko au proxy,
  limitation des tentatives de login. Les accès HTTP applicatifs ne journalisent
  pas les corps, mots de passe ni cookies. Les secrets de ce service ne vont pas
  dans la mémoire partagée.
- Le service worker conserve seulement l’interface publique et les icônes.
  Aucun feed, identifiant ou décision n’est mis en cache hors ligne. Aucune
  approbation n’est mise en attente pour un envoi ultérieur hors connexion.

## Publication et exploitation

DNS existant autorisations.example.org → VPS OVH. Une règle HAProxy exacte par SNI
atteint nginx local sur le VPS, certificat ACME dédié. Un tunnel SSH chiffré
entre serveurs joint le Dell. Aucun VPN n’est demandé aux clients. Le routage
existant des autres domaines reste inchangé.

Services : ops-approvals.service, ops-approvals-export.service (système),
ops-approvals-tunnel.service (utilisateur ops-user, démarrage avec sa session
utilisateur persistante). Les sessions sont conservées après relance du service.
Le certificat est géré par certbot ; le hook nginx existant recharge les
certificats renouvelés.

Vérification sans authentification : GET /healthz. Répond 503 si l’export est
absent ou périmé. Une panne du Dell ou du tunnel rend le feed indisponible ;
aucune décision n’est inventée. Le circuit Zulip d’origine reste inchangé.

Installation initiale : deploy/install-local.py après récupération des wheels
versionnées par requirements.txt. Publication : deploy/publish-vps.py ; ce
script archive la configuration HAProxy et revient en arrière si sa validation
échoue. Archive VPS : /var/lib/ops-approvals-publication. Avant toute mise à jour,
conserver la version actuelle et ses empreintes. Ne jamais écraser une archive
historique pour rejouer une publication.

Retour arrière manuel : retirer uniquement le backend/ACL ops_approvals et
ses deux vhosts nginx après contrôle de dérive ; tester les configurations puis
recharger les services. Arrêter le tunnel et les deux services locaux. Conserver
les données de session en privé et les archives. Le binaire Tauri précédent est
archivé dans artifacts/approvals-pwa-2026-09-13/ops-desktop-before-0.6.19.

## Recette du 13 septembre 2026

- Backend : 12 tests, dont autorisation utilisateur, origine, cookies, chiffrement,
  révocation, anti-forgery, décisions en conflit, expiration, projection périmée,
  mise à plus tard et historique des sujets résolus. La décision simulée passe
  par le véritable ApprovalProcessor du bridge avec broker de test isolé.
- Connexion et lecture réelles : 14 demandes historiques lues avec le compte
  Zulip existant, états acceptés/expirés ; déconnexion révoquée vérifiée.
- UI Chromium : 390, 768 et 1440 pixels, aucun débordement horizontal, expansion,
  mise à plus tard et acceptation simulées ; aucune erreur JavaScript.
- Tauri/WebKit réel : onglet après Zulip, chargement HTTPS, formulaire de connexion
  visible, commandes natives refusées à cette vue distante. Profil de test isolé.
- PWA : interface disponible hors ligne, aucune réponse API privée en cache.

Les tests ne prennent aucune décision sur une action de production. La validation
physique sur iPhone/Android reste à faire sur ces appareils ; les tests responsive
ne constituent pas une certification de tous les navigateurs mobiles.

Références officielles :
- https://dev.zulip.com/api/fetch-api-key
- https://chat.zulip.com/api/add-reaction
- https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable

Correction interface du 13 septembre : liens « Voir dans Zulip » et bouton
« Installer l’application » supprimés à la demande de Louis. Synchronisation
Zulip et capacités PWA conservées. Cache public versionné v2.

Le bouton Actualiser interne est retiré à la demande de Louis. Seul le
bouton de rechargement de la barre Tauri reste visible ; synchronisation
automatique conservée. Cache interface v3.
