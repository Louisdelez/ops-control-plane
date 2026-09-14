> Mise à jour 0.6.17 : la connexion Google native est rétablie sur demande de
> Louis. Voir [connexion-google-ops.md](connexion-google-ops.md). Le repli forcé
> vers Firefox décrit ci-dessous correspond à l’ancienne version 0.6.16.

# Portails fournisseurs dans Ops — 13 septembre 2026

Ops 0.6.16 ajoute les portails des deux fournisseurs actuellement retenus :

- DeepSeek : https://platform.deepseek.com/balance (solde, recharge, consommation et clés via la navigation officielle).
- Jina : https://jina.ai/api-dashboard/ (compte, clé, consommation et recharge commune embeddings/reranker).

Les onglets se règlent dans Paramètres > Fournisseurs API. Leur visibilité
persiste dans provider-tabs.json, sans identifiant ni secret. La barre défile
horizontalement lorsque la fenêtre est étroite ; les paramètres restent à droite.
Les pages sont celles des fournisseurs : Ops ne reconstitue pas leur facturation.
Aucun achat ou rechargement n’a été effectué par l’agent.

## Comptes et sessions

Les vues partagent le répertoire WebKit persistant d’Ops. Les cookies persistants
et le stockage local sont conservés après fermeture ; les règles d’expiration,
la déconnexion et les contrôles de sécurité restent ceux du fournisseur.

Le trousseau Fedora accepte aussi la connexion directe DeepSeek sur
platform.deepseek.com/sign_in. Capture limitée aux champs de connexion observés,
sauvegarde après navigation vers balance/usage/api_keys, remplissage à la prochaine
connexion. Les pages Google, les formulaires de paiement et les clés API ne sont
pas capturés. Modification/oubli dans Paramètres > Mots de passe et connexions.
Les entrées Zulip et OpenBao sont préservées.

Jina propose Google, GitHub ou un parcours par e-mail. Aucun mot de passe Jina
n’a été inventé ou assimilé à sa clé API. La session web peut être conservée ;
les connexions fédérées sont traitées par le navigateur du compte.

## Limite Google explicitement annoncée

Google interdit OAuth dans une WebView embarquée contrôlée par une application :
https://developers.google.com/identity/protocols/oauth2/policies

Le bouton navigateur ↗ et les boutons Google reconnus ouvrent le portail officiel
dans le navigateur habituel. La connexion doit commencer dans ce navigateur pour
que le contexte OAuth et les cookies de retour soient cohérents. Les requêtes
Google de navigation ou popup non initiées par ce bouton sont bloquées, sans
ouverture automatique du navigateur. L’iframe de session Firebase sur la même
origine Jina reste autorisée. Aucune imitation de
navigateur, extraction de cookies, interception de jeton ou stockage de mot de
passe Google dans Ops. La session Google reste dans le navigateur et n’est pas
transférée à la WebView. Ce n’est donc pas une connexion Google intégrée à Ops.
Le compte fournisseur connecté par Google doit être utilisé dans ce navigateur.

## Cloisonnement

Les deux vues fournisseurs n’ont aucune capacité IPC Tauri. Leurs navigations
restent sur leur origine HTTPS exacte ; une navigation vers les services locaux
est refusée. Les liens HTTPS externes sont ouverts dans le navigateur. Le canal
natif du bouton Google ne peut ouvrir que le portail fixe de sa propre vue ;
il ne reçoit aucun identifiant ou jeton. Les commandes de réglage sont réservées
à la page locale Paramètres ; les commandes de portail aux onglets/paramètres.

## Vérification et limites

Preuves : artifacts/provider-tabs-2026-09-13/.
Tests unitaires des origines et fournisseurs, capture/remplissage sur formulaires
synthétiques Zulip et DeepSeek avec entrées Secret Service temporaires isolées.
WebDriver utilise un répertoire de données temporaire, sans compte réel : pages
publiques, persistance cookie/localStorage après relance, visibilité des onglets,
refus des commandes locales aux fournisseurs et barre à largeur 860 pixels.

Aucune authentification réelle Google/DeepSeek/Jina ni opération de paiement
n’a été effectuée par l’agent. Les tests de persistance synthétique ne garantissent
pas la durée de validité d’une session authentifiée chez un fournisseur.
