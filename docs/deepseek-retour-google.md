# Retour Google DeepSeek — Ops 0.6.18

Correction installée le 13 septembre 2026 à la demande de Louis.

L’ancienne session restait sur accounts.google.com/CheckCookie avec une page
blanche. Une nouvelle tentative native reproduisait un retour DeepSeek sign_in
avec l’erreur STATE_INVALID. Aucune navigation refusée n’était enregistrée.
Le réglage WebKit par défaut observé était no-third-party.

Le contexte WebKit intégré accepte désormais les cookies tiers nécessaires au
parcours SSO avant la navigation des fournisseurs. Cela s’applique au contexte
partagé d’Ops ; ce n’est pas une exception limitée à un domaine. Firefox reste
inchangé. Les validations OAuth/CSRF des fournisseurs restent intactes.

Après ce seul ajustement de compatibilité, le parcours Google avec le compte déjà
mémorisé a abouti à platform.deepseek.com/usage. Les contrôles Usage, API keys et
Top up étaient présents. Après fermeture normale puis relance d’Ops, cette même
page authentifiée est revenue sans nouvelle authentification. Aucun paiement,
rechargement ou changement de clé n’a été effectué.

Les observations utilisent les métadonnées d’accessibilité : hôte, chemin et noms
de contrôles connus. Aucun mot de passe, code OAuth, jeton ou cookie n’est présent
dans les preuves. Le diagnostic local conserve au plus 32 événements expurgés
(hôte, catégorie de route, événement), en fichier privé.

Validation : 11 tests ordinaires réussis, compilation release réussie, parcours
Google réel réussi et persistance après relance vérifiée. La session reste soumise
à son expiration ou sa révocation côté fournisseur. Le parcours Jina authentifié
n’a pas été revérifié dans cette correction DeepSeek.

Preuves : artifacts/deepseek-return-2026-09-13/. Ancienne version conservée pour
retour arrière ; profil et données utilisateur préservés.
