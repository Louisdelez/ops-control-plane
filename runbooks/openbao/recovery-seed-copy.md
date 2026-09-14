# Copie initiale de récupération chiffrée

`backup.recovery-seed-copy.v1`, classe C, exige une approbation Zulip séparée pour chaque destination (`nas` ou `edge-vps`). Le helper est installé localement ; sa présence ne vaut pas accord.

La release fixe les SHA-256 de trois fichiers age du 11 septembre 2026 : snapshot OpenBao 13:53 UTC, parts de déverrouillage chiffrées, bundle Zulip 13:53 UTC. Leur déchiffrement avec la nouvelle identité a été vérifié localement avant préparation. La clé privée n’est pas incluse. Aucune clé ancienne n’est récupérée par cette opération.

L’action utilise l’alias SSH natif et la clé d’hôte déjà connue, sans sudo distant. Elle crée dans le HOME du compte distant `ops-encrypted-backups/dell-seed-<SHA256>`, avec répertoires 0700 et fichiers 0600. Le récepteur refuse les liens symboliques, les membres tar inattendus, les chemins sortants, les fichiers non-age et les conflits. Une copie identique est vérifiée, pas remplacée. Le retour contient les trois empreintes contrôlées. En cas d’interruption, consulter l’action avant toute reprise.

Impact : moins de 96 Mio par destination au maximum, uniquement un nouveau dossier de sauvegardes chiffrées. Aucun service, firewall, base ou configuration distante n’est modifié. Retour arrière : conserver le dossier inerte ; toute suppression éventuelle fera l’objet d’une action distincte. Il ne faut pas supprimer les sauvegardes existantes.

Le VPS constitue une copie hors domicile ; le NAS une copie hors Dell. Ce transfert initial ne remplace ni la sauvegarde périodique, ni la remise de la clé sur un support séparé, ni une restauration complète isolée. Aucun de ces contrôles n’est présenté comme acquis par une empreinte de copie correcte.
