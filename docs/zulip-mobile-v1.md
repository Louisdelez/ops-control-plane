# Zulip mobile V1

Le bridge local couvre les gestes mobiles sans exposer une API générale :

- lancer une mission compartimentée avec `!ops mission <projet> <titre>` ;
- demander son état avec `!ops statut <uuid>` ;
- remettre en file une mission en pause avec `!ops reprendre <uuid>` ;
- répondre à une question d’équipe avec `!ops répondre <uuid> <texte>` ;
- récupérer le rapport quotidien avec `!ops rapport` ;
- récupérer une capture PNG déjà produite et validée avec
  `!ops capture <uuid>` ;
- approuver/refuser une action de classe C par les réactions Unicode officielles
  ✅/❌.

Chaque commande doit être envoyée dans un sujet du stream d’approbation. Le
sujet sert de thread de mission. Le bridge refait un lookup du message et de
l’utilisateur, vérifie les IDs numériques, l’état humain actif, le stream et
l’identité du bot. Une commande textuelle ne peut jamais fournir son propre
acteur.

Le compte broker `zulip-mobile` peut seulement voir/créer des missions dans les
projets explicitement accordés et écrire `mission.status`/`mission.record` ; la route de reprise
n’accepte que `paused -> open` et refuse une mission terminée. Aucun endpoint de
runbook, incident, secret ou exécution n’est présent dans l’UDS de production.

Les réponses sont bornées et idempotentes. Les pièces jointes sont limitées au
rapport JSON déterministe et au chemin PNG dérivé d’un UUID de mission. Aucun
chemin fourni par la conversation n’est utilisé.

Cette couche reste volontairement dormante tant que le realm HTTPS, le bot et
les IDs numériques humains/streams ne sont pas réellement renseignés dans
OpenBao puis validés par `install-zulip-bridge.sh --check`. L’installation
locale ne crée ni organisation Zulip, ni compte iPhone, ni token fictif.
