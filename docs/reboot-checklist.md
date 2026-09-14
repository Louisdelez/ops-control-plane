# Redemarrage Fedora / NVIDIA 580xx — resultat

Le noyau `7.1.13-200.fc44.x86_64` et le pilote RPM Fusion NVIDIA 580.178.04
compatible Pascal sont installes. Le module est construit, signe, charge et son
signataire est enrole dans MOK. Secure Boot reste actif, `nouveau` est absent et
`nvidia-smi` reconnait la GTX 1050.

1. Le redemarrage de validation est termine.
2. Les dix gates post-redemarrage ont passe.
3. La campagne bornee a reproduit un pic systeme a `90,050 C` ; la garde
   thermique a arrete Ollama.
4. Le benchmark corrige s'est arrete fail-closed en code `75`, avec zero
   promotion et des preuves conservees.
5. Ollama reste inactif. Aucune seconde relance n'est autorisee dans cette
   intervention.

Le marker courant est `interrupted` avec le code `75`. Il ne doit etre rearme
qu'apres diagnostic du refroidissement et decision humaine explicite. Un nouvel
essai devra conserver les seuils 80/85/90 C et reduire encore la charge. Le
rapport de cette campagne est
`post-reboot-nvidia580-v1-20260904T183131Z/report.json`.
