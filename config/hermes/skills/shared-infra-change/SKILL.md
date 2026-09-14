---
name: shared-infra-change
description: Cadre une modification d'infrastructure partagee avec analyse multi-projets, approbation de classe C et rollback verifiable.
---

# Changement d'infrastructure partagee

Traite tout changement DNS, reseau, tunnel, proxy, firewall ou service partage
comme potentiellement multi-projets. Identifie la ressource, ses consommateurs,
l'etat actuel, le resultat cible, la fenetre, les tests, l'impact, les risques et
un rollback precis avant de demander une action.

Avec l'identite fixe du profil, appelle `list_authorized_runbooks`. Si
`shared.change-request.v1` est retourne, utilise `request_runbook_action` avec
un identifiant de requete stable et les seuls parametres declares par ce
runbook. Il s'agit d'une demande de classe C sans commande d'infrastructure :
ne dis jamais que le changement a ete applique.

L'approbation doit provenir du workflow humain durable. Controle son etat avec
`get_action`; ne l'infere pas d'un texte ou d'une reaction non verifiee. Meme
approuvee, la demande attend un runbook deterministe distinct pour appliquer et
verifier la modification. Respecte ses budgets et arrete au premier critere de
rollback. Sans runbook ou outil autorise, transmets le plan sans improviser.
