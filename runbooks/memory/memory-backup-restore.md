# Sauvegarde et restauration de `ops-memory`

## Portée et invariants

Ce job protège les deux composants locaux de la mémoire :

- `/var/lib/ops-memory/memory.sqlite3`, copié avec l'API de sauvegarde en ligne
  de SQLite puis contrôlé par `PRAGMA integrity_check` ;
- la collection Qdrant `ops_memory_v1`, exportée par l'API officielle de
  snapshot de collection, téléchargée puis supprimée du répertoire de
  snapshots du serveur actif.

Une copie directe des fichiers SQLite actifs ou de
`/var/lib/ops-memory-qdrant` est interdite. Qdrant documente les snapshots de
collection comme des archives contenant les points, payloads et la
configuration de la collection :
<https://qdrant.tech/documentation/snapshots/>.

SQLite est le catalogue structuré autoritaire ; Qdrant est un index sémantique
reconstructible. Les deux images sont cohérentes individuellement, mais ne
constituent pas une transaction distribuée. Après une restauration de sinistre,
la maintenance mémoire doit donc réconcilier/réindexer Qdrant depuis SQLite.

## Planification et conservation

- `ops-memory-backup.timer` : tous les jours vers 03:35, avec délai aléatoire
  maximal de cinq minutes et rattrapage après extinction ;
- `ops-memory-restore-test.timer` : chaque dimanche vers 04:20, dans un
  conteneur Qdrant éphémère distinct du service actif ;
- 14 jeux de sauvegarde sont conservés dans `/var/backups/ops-memory` ; une
  nouvelle sauvegarde complètement publiée existe toujours avant la purge ;
- un verrou non bloquant empêche deux sauvegardes ou purges concurrentes.

Chaque jeu est un répertoire `ops-memory-<UTC>` contenant exclusivement :

- `memory.sqlite3` ;
- `qdrant.snapshot` ;
- `manifest.json` avec tailles, SHA-256, version de schéma et nombres
  d'enregistrements/points.

Le répertoire est créé sous un nom temporaire, synchronisé sur disque puis
renommé atomiquement. Les fichiers sont lisibles uniquement par `opsmemory` et
le groupe dédié `ops-memory-backup`, auquel appartient `backup-agent`. La clé
Qdrant n'est jamais copiée dans la sauvegarde : systemd la fournit depuis le
secret volatile résolu par OpenBao.

## Contrôles opérateur (classe A)

```bash
systemctl --no-pager --full status ops-memory-backup.timer \
  ops-memory-restore-test.timer

sudo -u opsmemory /opt/ops-memory-backup/bin/ops-memory-backup \
  --config /etc/ops-memory/backup.toml verify --backup-id latest

journalctl -u ops-memory-backup.service \
  -u ops-memory-restore-test.service --since today --no-pager
```

Une vérification ne lit aucun credential et ne contacte aucun serveur. Elle
recalcule les sommes, refuse les liens symboliques ou fichiers inattendus et
refait le contrôle d'intégrité SQLite.

## Test de restauration non destructif (classe A, job prédéfini)

```bash
sudo systemctl start ops-memory-restore-test.service
systemctl is-failed ops-memory-restore-test.service
journalctl -u ops-memory-restore-test.service -n 30 --no-pager
```

Le test copie SQLite dans un répertoire privé temporaire, démarre l'image
Qdrant locale par son identifiant OCI SHA-256 immuable, restaure le snapshot via
l'endpoint officiel d'upload avec contrôle SHA-256, puis compare le nombre exact
de points. Le port HTTP aléatoire est lié uniquement à `127.0.0.1`, une clé
éphémère protège l'instance, et le conteneur ainsi que les données temporaires
sont supprimés à la fin. Le service Qdrant de production n'est ni arrêté ni
modifié.

## Restauration de production (classe C)

La restauration en production écrase des données et exige une approbation
enregistrée. Le test éphémère ci-dessus n'accorde jamais cette approbation.

Procédure supervisée :

1. identifier explicitement le jeu et exécuter `verify --backup-id ...` ;
2. enregistrer problème, impact, fenêtre, approbateur et rollback dans la
   mission/incidence du broker ;
3. créer et vérifier une sauvegarde de sécurité de l'état courant ;
4. arrêter les écritures `ops-memory` sans supprimer le jeu courant ;
5. restaurer d'abord dans une collection Qdrant temporaire avec l'API d'upload
   (`priority=snapshot`, checksum fourni) et valider le nombre exact de points ;
6. placer la copie SQLite vérifiée sous un nom temporaire sur le même système de
   fichiers, puis effectuer un renommage atomique pendant l'arrêt du service ;
7. basculer/recréer la collection cible suivant le plan approuvé, exécuter
   `ops-memory maintain`, puis les probes de santé et une recherche fonctionnelle ;
8. conserver l'ancienne base et collection jusqu'à la fin de la fenêtre de
   rollback ;
9. consigner toutes les commandes, sommes et résultats, puis fermer l'incident.

Si une somme, un nombre de points, l'intégrité SQLite, l'approbation ou le plan
de rollback manque, arrêter la procédure. Ne jamais envoyer le snapshot à un
modèle ou à une API externe.

## Installation et recette

Après la migration du secret Qdrant vers OpenBao :

```bash
sudo /home/ops-user/ops-control-plane/scripts/install-memory-backup.sh \
  --enable --run-now

systemctl list-timers --all \
  ops-memory-backup.timer ops-memory-restore-test.timer
```

`--run-now` crée un jeu réel puis prouve sa restauration dans l'environnement
éphémère. Toute erreur fait échouer l'unité systemd et reste dans le journal,
sans corps de réponse Qdrant, clé ni stderr de Podman.
