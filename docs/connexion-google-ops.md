> Mise à jour 0.6.18 : retour Google DeepSeek et persistance réelle après relance
> vérifiés. Voir [le diagnostic du retour Google](deepseek-retour-google.md).

# Connexion Google native — correction Ops 0.6.17

Louis a demandé de garder la connexion Google dans les onglets Ops et a signalé
que l’affirmation d’un blocage systématique était incorrecte. La version 0.6.16
bloquait elle-même les navigations Google et interceptait les boutons pour ouvrir
le navigateur externe. Ces interceptions sont retirées en 0.6.17.

## Fonctionnement

- Le parcours officiel du fournisseur s’exécute sans modifier le User-Agent.
- DeepSeek peut rediriger son onglet vers le formulaire Google puis son callback.
- Une popup demandée par Jina est une vraie WebView liée à la vue appelante via
  les window_features Tauri : contexte WebKit, cookies et relation d’ouverture
  sont conservés pour les communications du fournisseur.
- Les fenêtres de connexion sont limitées au fournisseur d’origine et aux hôtes
  HTTPS précis utilisés pour l’authentification (Google Accounts, vérification
  accounts.youtube.com, Firebase Jina). Pas de navigation vers les services locaux.
- Aucune capacité IPC locale, aucun script de capture de mot de passe Google,
  aucune copie de cookies depuis Firefox et aucune extension de navigateur.
- Les redirections en arrière-plan ne déclenchent plus Firefox. Le bouton
  navigateur reste une option explicite pour l’utilisateur.
- Fermer une popup ne ferme pas les terminaux : seul l’événement de destruction
  de la fenêtre principale déclenche leur fermeture.

## Persistance

Le contexte WebKit persistant d’Ops conserve les données de session selon les
règles et durées de validité de Google et du fournisseur. Le trousseau natif reste
réservé aux mots de passe directs déjà pris en charge. Une session Google ne
nécessite pas de stocker son mot de passe dans le trousseau Ops.

## Vérification

Tests : artifacts/native-google-2026-09-13/.
Les essais utilisent un profil temporaire, sans identifiant réel, sans paiement
et sans connecter un compte. L’affichage du formulaire Google ne prouve pas
encore la réussite de l’authentification du compte de Louis ni sa durée de validité.
Les pages, popups et résultats observés sont conservés dans les preuves.

Les conclusions de 0.6.16 dans fournisseurs-ops.md et les anciens handoffs sont
historiques : elles ne doivent pas servir à réintroduire un blocage logiciel
sur la seule présomption que Google refuserait le parcours.
