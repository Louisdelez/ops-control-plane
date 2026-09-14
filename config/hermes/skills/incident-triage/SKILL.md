---
name: incident-triage
description: Triage un incident d'exploitation avec preuves, runbooks autorises, budget borne et escalade en cas d'incertitude.
---

# Triage d'incident

Reste en lecture tant que les faits, le projet et l'incident ne sont pas
identifies. Ne deduis jamais qu'un service est sain sans resultat deterministe.

1. Si un identifiant est fourni, relis l'incident avec `get_incident` et
   l'identite fixe du profil. Sinon, appelle `list_open_incidents` pour le
   projet autorise et selectionne uniquement sur des faits non ambigus. Cree un
   incident seulement si aucun enregistrement existant ne correspond. Une fois
   selectionne, appelle `list_actions_for_incident` et lis uniquement l'action
   pertinente avec `get_action`.
2. Appelle `list_authorized_runbooks` avant toute action. N'utilise qu'un
   runbook effectivement retourne et ne change jamais d'identite.
3. Pour une classe A, collecte un diagnostic borne puis consigne les preuves.
   Pour une classe B, respecte le budget du runbook, verifie le resultat et
   arrete apres un echec ou une recurrence. Ne contourne jamais un budget.
4. Pour une classe C, fournis cause probable, plan, impact, risques, duree et
   rollback. Demande l'action, puis attends une approbation humaine durable.
   Ne declare jamais une approbation ni un succes a partir d'un message seul.
5. Si aucun runbook ne correspond, si les preuves se contredisent ou si un
   outil attendu est absent, n'improvise pas : produis un court transfert a
   l'humain avec les faits connus et la prochaine verification suggeree.

Ne revele aucun secret et ne propose aucune commande libre.
