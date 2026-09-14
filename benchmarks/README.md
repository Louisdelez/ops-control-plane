# Benchmark Ollama du plan de controle

Ce banc mesure des modeles Ollama deja installes. Il ne contient aucun appel a
`/api/pull`, ne telecharge rien et ne modifie jamais la configuration active.
Une execution produit seulement un rapport JSON et Markdown sous
`benchmarks/results/` (repertoire ignore par Git).

Les scenarios couvrent quatre comportements obligatoires : routage vers le bon
compartiment, refus d'une action hors scope, escalade en cas de doute ou
d'approbation requise, et choix d'un runbook borne. Une reponse n'est acceptee
que si elle respecte strictement `decision.schema.json`.

## Lancer un benchmark reel

Verifier d'abord les modeles deja presents :

```bash
ollama list
```

Puis lancer :

```bash
python3 benchmarks/ollama_benchmark.py \
  --config config/ollama/benchmark.json \
  --scenarios benchmarks/prompts.json \
  --schema benchmarks/decision.schema.json \
  --model qwen3:0.6b
```

`--model` compare le nom exact a la liste des candidats et peut etre repete.
Il filtre uniquement la configuration en memoire : le fichier source et la
selection de production restent inchanges.

Le serveur doit etre une URL HTTP loopback. Les modeles absents sont notes
`not_installed`; ils ne sont jamais recuperes automatiquement. Le premier appel
sert d'echauffement, puis chaque scenario est repete selon la configuration.

Sur le Dell thermiquement contraint, chaque requete utilise `num_thread: 2` et
`num_batch: 128`. Avant l'echauffement comme avant chaque mesure, la campagne
attend trois lectures consecutives a 80 C ou moins, espacees de 5 secondes. Le
delai de refroidissement est borne a 300 secondes. Ces limites ne remplacent
pas la garde root : elle continue d'arreter Ollama des 90 C. L'eligibilite du
benchmark est plafonnee a 85 C afin de conserver une marge avant cet arret dur.

Les rapports contiennent notamment : latence totale et premier token,
tokens/seconde renvoyes par Ollama, RSS cumulee des processus Ollama, memoire
GPU, temperatures GPU/systeme, taux de conformite au schema et score
fail-closed. Une metrique obligatoire indisponible est un echec de gate.

`promotion_eligible` est uniquement un resultat d'evaluation. Le champ
`promotion_performed` reste toujours `false`; aucun fichier de selection, aucun
service et aucun modele ne sont modifies. La promotion reste une decision
humaine apres lecture du rapport.

Une disparition d'Ollama, un flux termine sans chunk `done:true`, une mesure
thermique indisponible ou l'expiration du refroidissement interrompt toute la
campagne avec le code 75. Le rapport partiel porte alors
`campaign.status: interrupted`; toutes les eligibilites, y compris celles de
modeles termines plus tot, sont forcees a `false`.

## Tests hors ligne

```bash
python3 -m unittest discover -s benchmarks/tests -v
```

Les tests utilisent `fixtures/simulated_responses.json`. Ils n'ouvrent aucun
socket et ecrivent leurs rapports uniquement dans un repertoire temporaire.

Pour verifier aussi le CLI et generer un rapport de demonstration sans Ollama :

```bash
python3 benchmarks/ollama_benchmark.py \
  --config benchmarks/fixtures/simulated_config.json \
  --simulate benchmarks/fixtures/simulated_responses.json \
  --run-name simulation-manuelle
```
