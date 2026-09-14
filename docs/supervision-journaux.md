# Collecte et archives des journaux

La vue **Infrastructure → Journaux** d’Ops est rendue dans Rust/Tauri. Elle
consulte une API Unix locale réservée à l’utilisateur de l’application. Aucune
fenêtre Grafana ni iframe externe n’intervient dans ce parcours.

## Deux modes complémentaires

La collecte continue récupère le journal systemd et les fichiers `json-file`
de Docker. Un curseur journal ou un couple inode/position permet la reprise.
L’archivage des événements et l’avancement du curseur partagent une transaction
SQLite : un échec n’avance pas la position, et un rejeu conserve les identifiants.
Chaque machine dispose de son worker ; les lots sont bornés à 200 messages et
les sources sont parcourues successivement pour éviter qu’un gros journal
monopolise la collecte. Une source rattrape son retard avant de revenir à un
rythme de quinze secondes. La découverte des conteneurs se renouvelle chaque
minute.

La lecture directe reste disponible pour interroger une période récente et un
service précis. Elle est bornée à 24 heures et 300 messages. Les messages lus
sont également conservés dans l’archive locale.

## Consultation et conservation

Les archives se parcourent par pages de 300 messages, triés par date puis
identifiant. Le bouton **Messages plus anciens** continue le même filtre, même
lorsque plusieurs événements ont exactement la même date. L’export JSONL porte
sur les messages affichés et filtrés, dans un fichier privé de l’utilisateur.

Aucun TTL et aucune purge automatique ne suppriment les événements archivés.
La base `logs.sqlite3` est comprise dans la sauvegarde chiffrée de télémétrie.
Cela nécessite du stockage disponible ; une archive durable n’est pas une
capacité de disque illimitée. Les quotas des destinations de sauvegarde restent
applicables et ne déclenchent aucune suppression automatique.

## États et limites explicites

La vue affiche le nombre de sources collectées, le rattrapage et les
interruptions enregistrées. Une source indisponible ne devient pas une source
vide saine. Un curseur disparu ou une rotation devenue illisible est conservé
comme incident de collecte dans `stream_gaps`, y compris après une reprise.
Une absence d’activité du collecteur pendant trois minutes rend son état périmé.

Le premier passage systemd commence une heure avant l’activation. Docker lit
les données encore présentes dans son fichier courant. Ce dispositif ne peut
recréer des journaux déjà supprimés sur une machine avant sa collecte. Les
rotations Docker non compressées encore présentes peuvent être rattrapées ;
une rotation perdue est signalée. Les pilotes Docker autres que `json-file`
restent explicitement non pris en charge par le collecteur continu actuel.

Le masquage automatique est appliqué avant transport. Les messages conservés
sont limités à 1024 caractères et le filtrage heuristique ne remplace pas une
politique applicative qui interdit de journaliser des secrets. L’archive reste
privée et ne doit jamais être copiée dans le dépôt public ni la mémoire des
agents.

## Déploiement et validation

Le répartiteur forcé SSH conserve ses cinq commandes précédentes et ajoute
une sixième commande fixe. Le contenu du collecteur est scellé ; les paramètres
ne permettent ni d’exécuter une commande arbitraire ni de choisir un chemin.
L’installation locale possède une archive et un retour arrière. L’extension
sur chaque serveur suit son action réellement approuvée dans Autorisations.

Les tests couvrent pagination, identifiants stables, rotation Docker, ligne
partielle, validation des entrées, curseur concurrent, transaction atomique et
conservation des signalements de trous. La recette Tauri couvre la consultation
réelle, l’état de collecte, les filtres et les exports. L’état d’activation de
chaque serveur doit être lu dans le broker et dans la vue de collecte ; la
présence du code ne prouve pas son déploiement.
