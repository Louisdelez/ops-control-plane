# Paramètres et trousseau Ops — 13 septembre 2026

Version installée : Ops 0.6.17. L’icône **Paramètres** reste tout à droite
de la barre supérieure. Elle ouvre une vraie page intégrée : navigation latérale,
Mots de passe et connexions, et À propos d’Ops. Le dialogue général GTK de
0.6.14 a été retiré à la demande de Louis. Les onglets et sessions restent ouverts.

## Utilisation

Se connecter normalement dans Zulip ou OpenBao. Le trousseau est actif par
défaut : Ops capture le formulaire de connexion puis mémorise les identifiants
après l’arrivée sur une route authentifiée. Aucun dialogue d’enregistrement
supplémentaire dans Ops. Le trousseau Fedora peut demander son déverrouillage.
Les accès précédemment enregistrés restent utilisables.

Dans **Paramètres généraux**, l’interrupteur **Activer le trousseau automatique**
contrôle l’enregistrement et la reconnexion. Ce choix persiste après redémarrage.
Désactiver conserve les entrées existantes et cesse la capture/le remplissage.
Les boutons **Gérer l’accès Zulip/OpenBao** permettent de modifier ou oublier
les identifiants et de désactiver la reconnexion pour un accès particulier.
Oublier n’interrompt pas une session web existante ; si le trousseau global
reste actif, une prochaine connexion réussie mémorisera à nouveau cet accès.

## Protection et fonctionnement

Stockage Secret Service/libsecret du bureau Fedora. Aucun fichier de mots de
passe supplémentaire. Le fichier local keyring-settings.json ne contient que
le booléen d’activation. Aucun mot de passe ingéré dans la mémoire des agents.

Capture limitée à l’origine HTTPS exacte et au formulaire username/password
sur /login/ de zulip.example.org ou /ui/vault/auth de127.0.0.1:8200 (userpass).
Le récepteur natif vérifie aussi l’URL réelle de la WebView et les tailles.
Les candidats restent temporairement en mémoire et expirent après180 secondes.
Une erreur de connexion restant sur le formulaire ne remplace pas une entrée
existante. La persistance attend la route authentifiée du service ; un mot de
passe incorrect n’est pas validé par un appel supplémentaire de l’agent.

Aucune commande IPC de lecture des secrets. Le canal WebKit dédié reçoit les
formulaires ; il ne rend aucun secret aux pages. Les mots de passe sont transmis
à secret-tool par stdin, jamais comme arguments ni comme journaux. Les dialogues
de gestion sont natifs et masqués. Une tentative automatique maximum par minute
évite une boucle en cas d’échec. Les saisies manuelles différentes sont préservées.

## Vérifications

Huit tests ordinaires passent. Test additionnel GTK/WebKit natif avec formulaire
synthétique : réception par le canal natif, absence de stockage avant succès,
puis stockage dans une entrée Secret Service isolée après navigation simulée
vers la route de succès, relecture et suppression. Aucun compte réel utilisé.

WebDriver sur le binaire release : ancien bouton absent, icône à droite,
page intégrée sans dialogue général, navigation et version vérifiées,
commande de modification refusée hors de la page locale, interrupteur appliqué
aux vues, réglage conservé après
fermeture et relance du processus. Tests de mauvais hôte/chemin et capture invalide.
La vraie connexion avec les identifiants saisis par Louis reste à observer.

Preuves actuelles : artifacts/settings-page-2026-09-13/ (install.json,
verification.json, page.png, tests.log, verify-settings.py). Test de capture
antérieur : artifacts/auto-keyring-2026-09-13/capture-integration.log.
Ancien binaire conservé ; Ops relancé
sans interrompre de terminal actif.

Références :
https://manpages.debian.org/unstable/libsecret-tools/secret-tool.1.en.html
https://webkitgtk.org/reference/webkit2gtk/stable/index.html

## Extension DeepSeek en 0.6.16

Le compte DeepSeek rejoint les accès gérés (connexion directe par mot de passe).
Capture sur l’origine platform.deepseek.com, chemin /sign_in, champs vérifiés
sur le formulaire officiel. Les routes /balance, /usage et /api_keys déclenchent
la sauvegarde de la saisie candidate. Les tests synthétiques vérifient aussi
le remplissage automatique ultérieur pour Zulip et DeepSeek.

Les sessions Jina et la limite Google sont détaillées dans fournisseurs-ops.md.
Aucun mot de passe Google n’est mémorisé dans Ops ; la session Google reste
celle du navigateur habituel. Installation et tests : artifacts/provider-tabs-2026-09-13/.

## Correction Google en 0.6.17

La connexion officielle Google reste désormais dans Ops (onglet ou popup liée).
Voir connexion-google-ops.md. Les indications de repli systématique vers le
navigateur dans la section historique 0.6.16 sont remplacées par ce fonctionnement.
Aucun mot de passe Google n’est capturé ; la persistance utilise la session WebKit.
