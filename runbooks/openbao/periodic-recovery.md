# Sauvegardes de récupération périodiques

La préparation locale `ops-recovery-bundle.timer` fonctionne tous les jours à
05:10, après les sauvegardes des composants. Elle refuse un composant de plus
de 30 heures, contrôle les SQLite, chiffre le lot avec age et vérifie son
déchiffrement par comparaison d’empreintes. Les snapshots OpenBao et Zulip
restent aussi chiffrés dans le lot. La clé privée age reste sur le Dell et n’est
jamais incluse. Les états des pilotes CLI, workflows et collecte mémoire sont
copiés avec l’API de sauvegarde SQLite, pas par copie brute de bases actives.

Ce lot de composants ne constitue pas une image complète du Dell. Il ne prouve
pas le redémarrage de toute la pile ni une restauration sur une autre machine.
Les composants sont sauvegardés successivement, sans transaction globale.

## Action à approuver, pour chaque destination

`backup.periodic-recovery-enable.v1` est une action de classe C avec un seul
paramètre : `resource=nas` ou `resource=edge-vps`. L’approbation autorise
l’activation durable de cette sauvegarde quotidienne précise, avec première
copie immédiatement vérifiée puis copie à 05:30 chaque jour. Elle ne donne
aucun droit de déploiement, suppression ou modification des services distants.

Destination : `~/ops-encrypted-backups/dell-daily-<empreinte>` sur le compte SSH
déjà configuré. Les clés d’hôte restent strictement vérifiées. L’archive et son
manifeste sont écrits dans un nouveau dossier privé, puis publiés atomiquement.
Une archive identique est vérifiée sans remplacement. Les conflits et liens
symboliques sont refusés. Aucun dossier antérieur n’est supprimé.

Limites : 256 Mio par archive, 20 Gio de sauvegardes quotidiennes par destination,
réserve de 512 Mio d’espace libre et cinq minutes par tentative. Le plafond
atteint interrompt la copie ; il ne provoque aucune purge. Les scripts de
transport sont figés par leurs SHA-256 lors de l’activation. Les changer impose
une revue de l’activation. L’échec de la première copie désactive le timer et
conserve les preuves pour revue.

Retour arrière : désactiver `ops-periodic-recovery@DESTINATION.timer` sur le Dell.
Les copies distantes restent inertes et conservées. Toute suppression future
nécessite une décision distincte. Les reçus locaux sont dans
`/var/lib/ops-periodic-recovery/` et les lots dans `/var/lib/ops-recovery-bundles/`.
