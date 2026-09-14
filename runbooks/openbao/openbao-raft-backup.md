# OpenBao — sauvegarde et test de restauration Raft

## Garanties V1

- `openbao-backup` est une identité Linux sans shell et sans privilège sudo.
- Son AppRole ne peut que lire `sys/storage/raft/snapshot` puis révoquer son propre jeton.
- Chaque jeton a exactement deux usages, une durée maximale de cinq minutes et reste lié à la boucle locale.
- Le snapshot HTTPS est envoyé directement dans `age`; aucun snapshot brut n'est créé sur disque.
- Seuls l'enveloppe `snapshot.age`, son SHA-256 et un manifeste non sensible sont publiés.
- Correction du 11 septembre 2026 : aucune clé privée correspondant à l’ancien destinataire n’a été retrouvée dans les emplacements inspectés, et le propriétaire confirme ne pas l’avoir reçue. Une nouvelle identité age a été générée et les nouvelles sauvegardes ont été déchiffrées avec succès. Le dossier privé `~/Documents/Recuperation-Ops-2026-09-11` contient son export ; une copie sur support indépendant reste à effectuer. Ne pas décrire cet export local comme une protection hors site.
- Les anciens snapshots sont archivés ; la nouvelle clé ne les déchiffre pas. Le seal OpenBao et les parts Shamir actives sont inchangés. Les deux parts TPM sont exportées uniquement dans une enveloppe age vérifiée. Le contrôle de déchiffrement et des SHA-256 du snapshot ne remplace pas une restauration complète.
- Une publication échoue si la révocation du jeton ne peut pas être confirmée.

## Installation et provisionnement initial

Depuis une copie revue du dépôt :

```console
sudo scripts/install-openbao-backup.sh
sudo systemctl stop openbao-raft-backup.timer
sudo /usr/local/sbin/provision-openbao-backup
sudo scripts/install-openbao-backup.sh --enable --run-now
```

Le provisionneur demande le mot de passe humain OpenBao de `ops-user` sans l'afficher ni le conserver. Il crée ou resserre la policy et l'AppRole, teste un téléchargement borné en mémoire vers `/dev/null`, révoque le jeton de test, puis chiffre RoleID, SecretID et accessor avec `systemd-creds --with-key=host+tpm2`. Il n'affiche aucune valeur.

Une rotation utilise la même commande, timer arrêté. L'ancien accessor est détruit après validation et publication atomique des nouvelles credentials. Une transaction ambiguë reste désactivée et doit être analysée dans les audits OpenBao.

## Contrôle quotidien

```console
sudo systemctl status openbao-raft-backup.timer
sudo systemctl status openbao-raft-backup.service
sudo journalctl -u openbao-raft-backup.service --since today
sudo find /var/backups/openbao -maxdepth 2 -type f -printf '%M %u:%g %p\n'
sudo sha256sum --check /var/backups/openbao/openbao-raft-*/snapshot.age.sha256
```

Chaque répertoire final doit contenir exactement :

- `snapshot.age`
- `snapshot.age.sha256`
- `manifest.json`

Le manifeste doit indiquer `age-x25519`, un hash identique, un flux non vide et `token_revoked: true`. La rétention locale garde 14 sauvegardes. Les alertes deviennent critiques sur échec de vérification et warning après 30 heures sans succès.

Copier ensuite les trois fichiers vers un stockage hors machine par un mécanisme séparé et une identité de transport dédiée. Cette V1 ne prétend pas fournir une sauvegarde hors site tant que ce transport n'est pas configuré.

## Test de restauration hors ligne

Ce drill se fait uniquement sur une machine ou VM isolée, jamais sur le nœud OpenBao actif. L'opérateur apporte volontairement la clé privée `age` depuis son support de récupération et vérifie son empreinte par une seconde source.

1. Vérifier le SHA-256 avant déchiffrement.
2. Monter un `tmpfs` privé, mode `0700`, d'une capacité suffisante.
3. Déchiffrer `snapshot.age` vers un fichier mode `0600` dans ce `tmpfs`.
4. Démarrer un OpenBao de test compatible, initialisé avec la même configuration de seal.
5. Exécuter `bao operator raft snapshot restore SNAPSHOT` sans option de contournement.
6. Unseal, vérifier `/v1/sys/health`, la présence attendue des mounts et une lecture canari non sensible.
7. Arrêter l'instance, supprimer le fichier déchiffré, démonter le `tmpfs` et retirer la clé privée.
8. Archiver seulement date, versions, hash du snapshot, contrôles réussis/échoués et identité de l'approbateur.

Ne jamais utiliser `-force` : ce mode contourne les contrôles de cohérence liés au seal. Si une restauration normale échoue, conserver les artefacts chiffrés et escalader avant toute autre action.

## Incident

- Échec unique : collecter statut, journal et métrique; ne pas lancer de boucle de retry.
- Deuxième échec ou sauvegarde vieille de plus de 30 heures : incident `Backup-Shared`, priorité warning au minimum.
- Hash, manifeste ou enveloppe `age` incohérents : incident critique; ne pas supprimer les précédentes sauvegardes valides.
- Soupçon de fuite : arrêter le timer, lancer le provisionneur pour rotation contrôlée, vérifier l'audit OpenBao et faire approuver toute rotation de la clé de récupération.
