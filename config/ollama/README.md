# Configuration Ollama du benchmark

`benchmark.json` decrit des candidats, pas des modeles a installer. Le runner
interroge `/api/tags`, ignore tout candidat absent et n'appelle jamais
`/api/pull`.

Les quatre identifiants proposes couvrent les tailles du cahier des charges.
Ils restent des candidats jusqu'a un benchmark sur ce Dell. Si un tag local a
un autre nom, modifier uniquement le champ `model` apres verification avec
`ollama list`.

Les limites laissent environ 2,5 Gio de RAM au systeme et une marge VRAM. Elles
sont volontairement fail-closed : RSS, VRAM ou temperature manquante interdit
l'eligibilite. Cela evite de promouvoir un modele quand le pilote GPU ou les
capteurs ne permettent pas de confirmer le budget reel.

Le contexte est fixe a 4096 pour la premiere campagne. Une campagne 8192 doit
etre un fichier de configuration distinct et ne doit etre comparee qu'a des
resultats obtenus avec les memes prompts, repetitions et limites.

Le fichier de configuration active d'Hermes ne doit jamais etre genere ou
modifie par le benchmark. Une selection eventuelle est revue et appliquee par
un humain apres validation de `report.json` et `report.md`.
