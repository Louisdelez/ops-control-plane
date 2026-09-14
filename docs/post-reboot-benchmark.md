# Reprise automatique NVIDIA/Ollama

Le service utilisateur `ops-post-reboot-benchmark.service` reprend une seule
fois le benchmark apres un redemarrage. Il reste fail-closed : il ne lance le
benchmark que si toutes les conditions suivantes sont vraies :

- `nouveau` est absent des modules charges ;
- Secure Boot est explicitement actif ;
- le module `nvidia` charge et son fichier correspondent tous deux a la branche
  `580xx` ;
- `modinfo` expose un signataire non vide et son nom commun (`CN`) correspond
  exactement au `CN` d'un certificat MOK enrole ;
- `nvidia-smi` voit au moins un GPU et ne rapporte que le meme pilote `580xx` ;
- les quatre entrees du benchmark installees sont des fichiers reguliers non
  modifiables par le groupe ou les autres ;
- l'API Ollama repond sur `127.0.0.1:11434` dans le delai borne.

Toutes les gates sensibles au demarrage sont reevaluees ensemble pendant ce
delai. Une activation NVIDIA ou Ollama legerement tardive ne fige donc pas un
faux echec au debut du boot.

Le service ne charge aucun module, ne modifie aucun pilote, ne telecharge aucun
modele et ne promeut aucun modele. Son environnement ne transmet aucune
variable de credential au benchmark.

## Installation utilisateur

Depuis le depot, sans `sudo` :

```bash
scripts/install-post-reboot-benchmark.sh
```

L'installateur copie un snapshot minimal du benchmark sous
`~/.local/share/ops-control-plane/post-reboot/`, installe l'unite sous
`~/.config/systemd/user/` et l'active pour le prochain demarrage du gestionnaire
systemd utilisateur. Il ne la demarre pas pendant la session courante.

## Etats et rapports

Le rapport synthetique est ecrit en mode `0600` dans :

```text
~/.local/state/ops-control-plane/post-reboot/status-v1.json
```

Il est limite a 16 Kio et ne contient ni sortie brute de commande, ni contenu
de prompt, ni variable d'environnement, ni credential. Le rapport detaille du
benchmark existant reste sous le snapshot installe dans
`benchmarks/results/<run-name>/`.

Le marker explicite suivant est cree atomiquement juste avant de lancer le
benchmark :

```text
~/.local/state/ops-control-plane/post-reboot/benchmark-v1.marker
```

Un echec de gate n'ecrit pas ce marker : un redemarrage ulterieur peut donc
reessayer apres correction du pilote. Une fois les gates franchis, le marker
reste present si le benchmark termine, echoue ou est interrompu. Une
interruption retryable est distinguee d'une campagne complete sans candidat :
elle porte l'etat `interrupted` et le code 75. Cette regle evite toute deuxieme
execution couteuse ou accidentelle.

Un rapport `benchmark_claimed` est ecrit immediatement apres la prise du marker.
Il reste donc une preuve lisible si le processus est interrompu avant le rapport
final.

Pour examiner l'etat :

```bash
systemctl --user status ops-post-reboot-benchmark.service --no-pager
journalctl --user -u ops-post-reboot-benchmark.service --no-pager
python3 -m json.tool ~/.local/state/ops-control-plane/post-reboot/status-v1.json
```

La disparition d'Ollama ou un flux sans marqueur final `done:true` arrete la
campagne. Aucun modele, meme termine avant cette interruption, ne reste
eligible. Le plafond de la garde systeme demeure 90 C, tandis que le plafond
d'eligibilite du benchmark est fixe a 85 C. Pour garder cette marge, le
benchmark attend avant chaque requete trois mesures consecutives a 80 C ou
moins et utilise deux threads avec un batch de 128.

Apres examen du rapport et du journal, une campagne portant explicitement
`interrupted` peut etre rearmee sans effacer ses preuves :

```bash
~/.local/libexec/ops-post-reboot-benchmark --rearm-interrupted
systemctl --user start ops-post-reboot-benchmark.service
```

Le rearmement copie le marker et le statut dans le sous-repertoire prive
`archive/<run-name>/`, puis retire le marker actif en dernier. Il refuse les
etats `claimed`, `completed`, `completed_no_eligible`, `failed` et tout couple
marker/statut incoherent. Il ne redemarre pas Ollama et ne se declenche jamais
automatiquement. Il faut donc d'abord laisser la machine refroidir, faire
redemarrer Ollama par le runbook borne autorise, puis confirmer sa disponibilite.

Le resultat historique du 4 septembre 2026 a ete produit par l'ancienne
version et porte a tort `completed_no_eligible` malgre l'arret thermique. Le
rearmement generique doit le refuser : son archivage/reclassement eventuel est
une migration unique, revue et bornee a ses empreintes exactes, pas une
exception permanente pour tous les codes 2.
