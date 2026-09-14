# Ops desktop

Application Rust/Tauri avec les onglets permanents **Atlas**, **Zulip**,
**OpenBao**, **Hermes**, **Terminaux** et **Infrastructure** en haut. Cliquer sur un onglet affiche sa vue web native.
Les vues natives restent ouvertes : changer d'onglet conserve la page, le
formulaire et la session en cours, sans rechargement.

Chaque service garde son interface et son authentification natives :

- Zulip : https://zulip.ops.local:8443/
- OpenBao : https://127.0.0.1:8200/ui/
- Hermes : http://127.0.0.1:9119/

Pas d'iframe, de copie d'interface ou de proxy d'authentification. Les vues
natives n'ont aucune permission Tauri ; seule la barre locale peut changer
l'onglet visible. Les certificats publics locaux sont approuvés pour leurs
hôtes respectifs. Aucun mot de passe n'est intégré à l'application.
Hermes présente le profil utilisateur officiel `~/.hermes`.

## Compiler et installer sur Fedora

Dépendances : cargo, rust, gcc-c++, pkgconf-pkg-config, openssl-devel,
gtk3-devel, webkit2gtk4.1-devel, librsvg2-devel.

```sh
cd src-tauri
CARGO_BUILD_JOBS=2 cargo build --release --locked
cargo test --release --locked
cd ..
python3 install.py
```

Chercher **Ops** dans les applications Fedora. Le lanceur démarre le service
utilisateur `hermes-dashboard.service`. L'UI OpenBao utilise l'option native
`ui = true` dans `/etc/openbao.d/zz-ops-desktop-ui.hcl`.

L'implémentation utilise les vues enfants Tauri (fonctionnalité `unstable`
de Tauri 2) et la disposition GTK native de Fedora : barre à hauteur fixe,
vue active extensible. Les versions exactes sont conservées dans Cargo.lock.

## Vérification

`python3 tests/native_webviews.py` utilise le binaire réel sous Xvfb avec
WebKitWebDriver/tauri-driver. Il clique les onglets, vérifie la conservation
d'une saisie témoin, le redimensionnement et le refus de l'IPC depuis les
pages des services. Aucune clé API ni aucun mot de passe n'est utilisé.

## Atlas intégré (0.3.0)

L'onglet **Atlas** réutilise `apps/model-manager` : son moteur Rust est une
dépendance locale, et son interface est embarquée par le build. Il n'y a pas
de seconde copie du catalogue ou du moteur de calcul à maintenir.

| Fonction d'origine | Adaptation dans Ops |
| --- | --- |
| Catalogue, filtres, fiches et prix par grille | Interface et calculs originaux |
| Scénarios de coût et comparaison | Calcul Rust original en micro-USD |
| Prévisualisation de routage | Classement original ; validation runtime conservée |
| Fournisseurs et comptes | Écrans originaux, états inconnus explicitement conservés |
| État et budgets | Lecture du socket existant ; projection des fournisseurs API du schéma natif ancien |
| Clés API | Pipe privé et validation originaux, écriture OpenBao sans activation automatique |
| Bootstrap et reprise des anciennes releases | État historique consultable ; opérations de réinstallation inaccessibles depuis Ops |
| Zulip | Onglet natif permanent |

L'orchestrateur installé ne fournit pas `/v1/model-performance` et
`/v1/provider-integrations`, et ne confirme pas la révision du catalogue Atlas.
Les performances, rapprochements des cartes et soldes détaillés ne sont donc
pas présentés comme vérifiés. Le catalogue et les simulations restent utilisables.
Les déclarations inactives de modèles locaux dans l'ancien service ne sont pas
proposées par l'adaptateur API ; aucun modèle local n'est démarré.

Le feature Rust `native-desktop` adapte uniquement l'usage intégré. L'ancienne
application autonome et ses sources de bootstrap sont conservées. Le helper
`ops-model-key-manager store-stdin` reçoit le protocole privé existant et
confirme seulement l'écriture dans OpenBao ; il ne redémarre aucun service et
ne contacte aucun fournisseur. La connexion OpenBao et les appels fournisseurs
réels restent à vérifier au moment où l'utilisateur ajoutera ses clés.

## Interface 0.4.0

La présentation Ops/Atlas utilise un thème clair et des accents verts sobres.
Atlas, Zulip, OpenBao, Hermes et les terminaux restent dans la barre permanente du haut. Dans Atlas,
une navigation latérale distingue Catalogue, Sélection, Coûts et Accès.
Les cartes présentent le prix du scénario, le fournisseur et le statut ;
la fiche conserve les grilles, limites, compétences et provenance détaillées.
Les fenêtres de configuration, états vides, filtres et vues compactes suivent
la même présentation. Les flèches, Début et Fin naviguent les onglets quand
la barre a le focus ; Ctrl+K reste disponible dans le catalogue.

Les interfaces natives Zulip/OpenBao/Hermes et le moteur métier sont conservés.
La nouvelle présentation réside dans `integration/embedded.css` et
`integration/embedded.js`, embarquées avec l'interface Atlas lors du build.

## Terminaux en colonnes — 0.6.2

L’onglet Terminaux occupe toute la largeur, sans barre latérale ni sélection manuelle CLI/API. Le bouton **+** propose **Codex** et **Claude Code** ; choisir un outil démarre immédiatement son interface CLI native. Chaque session occupe une colonne verticale indépendante, avec sa propre fermeture. Jusqu’à quatre sessions restent visibles côte à côte ; les fenêtres étroites permettent le défilement horizontal.

Les comptes et la connexion restent gérés par les CLI officiels. Les deux outils peuvent fonctionner simultanément. Un ancien réglage API exclusif devient automatiquement compatible avec les terminaux à leur ouverture. Les clés API déjà préparées par OpenBao sont détectées à l’ouverture de la vue et périodiquement ; la passerelle est alors activée avec les budgets existants. Une connexion CLI ne devient jamais une clé API fournisseur. La recette des fournisseurs et la préparation des clés restent à réaliser.

Chaque session conserve son PTY, son scope systemd utilisateur (1 500 Mio, 128 tâches), ses fichiers et comptes natifs. Fermer une colonne arrête uniquement sa session. Quitter Ops arrête toutes ses sessions. Aucune commande de contournement des permissions ni modèle local n’est ajouté ; les pages distantes restent exclues des commandes de terminal.

Les actions natives de connexion, reprise et pilote continu restent disponibles dans le backend pour les intégrations existantes ; le bouton + ouvre directement le CLI interactif demandé.

Validation : tests Rust avec vrai PTY et tests WebKit de lancement Codex/Claude, colonnes simultanées, limite à quatre, redimensionnement, fermeture indépendante, conservation lors du changement d’onglet et refus IPC des pages distantes. Les tests ne soumettent aucune demande au modèle.

## Défilement Zulip — Ops 0.6.6

Le WebKitGTK 2.52.5 du poste recevait les événements de molette sans déplacer le document Zulip quand la racine utilisait `overscroll-behavior: none`. Le test réel relevait `scrollY=826` avant et après huit crans ; remplacer cette propriété par `auto` permettait d’atteindre 138. Le script d’initialisation `src-tauri/zulip-scroll.js` applique uniquement cet ajustement à l’origine Zulip locale, sans modifier les autres onglets, les zones internes ou les verrous d’overflow des dialogues.

Un précédent de blocage de molette lié à cette propriété est documenté par WebKit : https://bugs.webkit.org/show_bug.cgi?id=245300 (ancien bug corrigé, pas une preuve que la version locale souffre exactement du même défaut). La correction présente repose sur la reproduction locale ci-dessus.

La recette `tests/zulip_scroll.py` utilise de vrais événements de molette X11 via WebKit WebDriver : montée/descente, retour d’onglet, rechargement et zone imbriquée. Elle ne publie aucun message ; si nécessaire, le chemin privé `OPS_SCROLL_CREDENTIAL_FILE` fournit la connexion sans journaliser le mot de passe. `OPS_SCROLL_EVIDENCE` choisit le dossier des résultats.

## Infrastructure (0.8.2)

L’onglet **Infrastructure** fournit une synthèse et les détails des cinq machines,
avec courbes CPU/RAM/réseau/E/S, barres de stockage inspirées de macOS, capteurs et processus filtrables.
L’historique est permanent, sauvegardé et archivé ; les relevés peuvent être
revus par date. Les 26 mesures avancées sont rendues dans la même page Infrastructure : catégories,
inspection interactive, légendes statistiques, agrandissement et export CSV privé.
La fenêtre Grafana de 0.8.0 a été retirée à la demande de Louis.
Voir [la supervision professionnelle](../../docs/supervision-professionnelle.md).
Voir [le guide du tableau de bord](../../docs/tableau-de-bord-infrastructure.md)
pour les mesures, leur fraîcheur, la conservation et les limites explicites.
La collecte continue lorsque la fenêtre est fermée. Aucun secret ni appel SSH
n’est accessible depuis la vue web ; seule la projection locale est lisible.

Dans **Infrastructure → Journaux**, récupération à la demande des journaux système,
services et conteneurs des cinq machines, recherche, niveaux, archives permanentes
et export JSONL privé. Limites et fonctionnement :
[guide de supervision](../../docs/supervision-professionnelle.md#journaux-des-machines-services-et-conteneurs).

Le design 0.8.2 ajoute un visuel original imagegen et des icônes Lucide embarquées
dans Infrastructure, avec thèmes clair/sombre et adaptation aux fenêtres étroites.
Voir [les sources du visuel et le prompt](ui/assets/ASSETS.md).
