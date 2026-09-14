---
name: backup-verification
description: Verifie un resultat de sauvegarde deterministe et organise une relance bornee ou une escalade sans inventer de preuve.
---

# Verification de sauvegarde

Une sauvegarde n'est validee que par des preuves du systeme deterministe :
identifiant du job, cible, horodatage, statut, taille ou manifest, controle
d'integrite et, lorsqu'il existe, resultat du test de restauration.

Le contrat `ops.verify_backup_evidence.v1` ne lit qu'un fichier de preuve local
allowliste : `status=success`, ressource correspondante, age borne et
`integrity_verified=true` lorsqu'exige. Il ne recoit ni mot de passe Restic, ni
token, ni cle. Une preuve absente, trop ancienne ou d'une autre ressource est
un echec, jamais un etat inconnu transforme en succes.

Le profil `backup-shared` possede une identite MCP bornee, mais aucun runbook de
sauvegarde ou de restauration n'est actuellement expose par le broker. Il peut
lire l'etat local et gerer son contexte de mission. Tant que
`list_authorized_runbooks` ne retourne pas un runbook de backup explicite,
resume les preuves deja fournies et escalade ; ne lance ni sauvegarde, ni
relance, ni restauration.

Si un profil futur dispose de `list_authorized_runbooks`, n'utilise qu'un
runbook retourne. Une relance doit etre explicitement autorisee, limitee par le
budget du runbook et suivie d'une nouvelle verification. Apres l'unique
tentative permise, tout nouvel echec impose l'arret et l'escalade.

Une restauration destructive ou un changement de retention exige un plan,
l'impact, la duree, les risques, un rollback realiste et une approbation humaine
durable. Ne revele aucun secret et ne fabrique jamais un statut de succes.
