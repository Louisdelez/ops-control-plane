# Modele de securite

## Invariants

1. Deny-by-default pour les outils, roles, projets et secrets.
2. Aucun endpoint ne retourne une valeur OpenBao a un modele.
3. Aucun shell libre : uniquement un `argv` produit par un runbook versionne.
4. Toute ecriture porte un `request_id`, une identite et un motif.
5. Les actions B ont un compteur et une fenetre ; le budget epuise force l'escalade.
6. Les actions C exigent une approbation non expiree d'un actor Zulip allowliste.
7. Le demandeur agentique ne peut pas approuver sa propre demande.
8. Suppression, rotation critique, restauration, migration et reseau partage sont C.
9. Les journaux sont lies par hash pour rendre une reecriture detectable.
10. L'absence de confiance ou de runbook est une escalade, jamais une improvisation.

## Risques materiels actuels

Le Fedora courant n'utilise pas LUKS. OpenBao chiffre son stockage Raft, mais cela
ne chiffre pas l'ensemble du poste, ses journaux ou ses artefacts. Les parts Shamir
et le root token ne doivent donc jamais rester en clair sur ce Dell. Une migration
vers Fedora chiffre LUKS reste la remediation correcte contre le vol du disque.

OpenBao utilise Shamir 3/2. L'auto-unseal local a été recetté avec deux parts
matérialisées comme credentials systemd chiffrés et liés à la clé hôte + TPM2 ;
le token root de bootstrap a été révoqué. Ce mécanisme permet le redémarrage du
control plane mais n'offre pas l'indépendance d'un KMS/HSM ou d'un transit seal
externe. Les parts de récupération humaines restent à conserver hors machine.

Le snapshot Raft quotidien est lu par un AppRole minimal, envoyé directement
dans `age`, hashé et manifesté sans persistance en clair. Le Dell ne conserve
pas la clé privée `age`. Une récupération exige donc l'artefact hors site, la
clé privée externe et un exercice dans une VM isolée ; aucune restauration ne
doit être tentée sur le nœud actif ni avec l'option `-force`.

La zone firewalld active et la zone par defaut sont toutes deux `ops-control`
avec cible `DROP`. Une nouvelle interface ne doit pas retrouver implicitement
la zone permissive FedoraWorkstation.

## Secrets historiques

Les cles SSH, WireGuard et tokens recuperes apres le reset sont des credentials de
bootstrap a rotation obligatoire. Le fichier SOPS historique ne peut pas etre
dechiffre tant que sa cle age hors ligne n'est pas retrouvee.
