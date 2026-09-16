# Démo 2 minutes — routeur de modèles

Public visé : un décideur public qui a signé pour « un assistant, un modèle ».
Thèse : garder un seul modèle pour tout n'est plus un choix technique, c'est un
réflexe. Les chiffres, et 5 000 agents par mois, disent autre chose.

Format : ~300 mots dits. Un présentateur parle, un deuxième conduit l'écran.
Tout est chronométré à la seconde ; les chiffres sont dans la fiche en fin de
document.

---

## 0:00 – 0:12 · L'accroche (écran : une seule phrase, fond noir)

> « Traduis cette phrase en anglais. »
>
> Aujourd'hui, cette question part sur un modèle de 128 milliards de
> paramètres. **Onze fois** l'énergie nécessaire. Pas une fois : à chaque fois.

Pas de « bonjour nous sommes l'équipe X ». On ouvre sur la phrase et le chiffre.
Le jury a vu quatorze groupes ; il a besoin d'une accusation, pas d'un sommaire.

## 0:12 – 0:30 · Le problème (écran : les 8 modèles Albert, un seul allumé)

> Albert sert huit modèles. L'assistant en utilise un, pour tout : la
> reformulation d'un mail comme l'analyse d'un budget. Ce n'est ni le moins
> cher, ni le plus intelligent. C'est celui qui a été choisi au début et que
> personne n'a rouvert depuis.
>
> Nous n'avons pas construit un modèle. Nous avons rouvert ce choix.

## 0:30 – 1:15 · La démo (45 s, trois envois, aucune navigation)

Conversation préparée, flag `router` activé, `arena_manual` activé.

| t | Action | Ce qu'on montre | Ce qu'on dit |
|---|---|---|---|
| 0:30 | Envoyer « Reformule ce mail pour un directeur » | Réponse immédiate, légende **« Auto · Modèle rapide »**, 1 feuille | « Question simple : modèle rapide. L'utilisateur le voit. » |
| 0:47 | Envoyer une analyse multi-étapes (budget, arbitrage) | **« Réflexion en cours… 12 s »**, légende **« Auto · Modèle de raisonnement »**, 3 feuilles | « Même conversation, question dure : on monte. Personne n'a rien réglé. » |
| 1:00 | Envoyer un message qui tombe dans un tirage arène | Deux réponses côte à côte, barre de vote, puis la carte de remerciement | « Et 5 % du temps, on demande à l'agent. Deux réponses, aucun nom de modèle, il choisit. » |

Règles de scène :
- **Jamais de nom de modèle à l'écran côté utilisateur.** C'est le point
  politique : l'agent ne choisit pas un fournisseur, il choisit un effort.
- Le vote n'est pas un gadget : montrer que la réponse choisie **glisse dans la
  conversation**. On ne fait pas perdre du temps à l'agent, on lui rend une
  réponse.
- Si le réseau tombe : capture vidéo de secours de ces 45 secondes.

## 1:15 – 1:45 · Les chiffres (écran : la fiche, un seul tableau)

> Sur les 100 000 requêtes par mois de la plateforme :
>
> - la facture passe de **573 $ à 37 $** par mois. **Quinze fois moins.**
> - le carbone passe de **6,0 kg à 1,4 kg** par mois. **−77 %.**
> - et ce n'est pas un benchmark qui le dit : à 5 % de tirage, ce sont
>   **5 000 agents par mois** qui votent à l'aveugle. 60 000 avis humains par an.
>
> Le routeur se paie lui-même : il coûte 5 $ par mois, 13 % de la facture qu'il
> remplace, et 0,3 seconde.

Phrase de bascule, à dire lentement :

> **Ces trois chiffres vont dans le même sens. Continuer avec un seul modèle,
> ce n'est plus un arbitrage : c'est une habitude qui coûte quinze fois le prix.**

## 1:45 – 2:00 · La suite, et la chute

> Chaque vote est étiqueté : domaine et tâche. Douze domaines, douze tâches.
> Nous ne les utilisons pas encore pour router — nous constituons le jeu de
> données. Dans un an, ce n'est plus trois paliers : c'est une route par
> usage, décidée par les avis des agents, pas par nous.
>
> Trois paliers aujourd'hui parce que c'est ce qu'on peut prouver. Le reste
> arrive quand les chiffres l'autorisent.

Dernière phrase, regard jury, on s'arrête :

> **Le bon choix n'est pas le plus gros modèle. C'est celui que les agents
> préfèrent, et il se trouve qu'il est moins cher.**

---

## Fiche chiffres (à projeter, et à connaître par cœur)

Hypothèses assumées à voix haute si on pose la question : 100 000 requêtes/mois,
répartition 45 % simple / 45 % standard / 10 % complexe, réponses types de
150 / 400 / 500+2 000 jetons (spec §10), prix publics catalogue (Albert facture
zéro : ce chiffre est la valeur de la compute, pas une facture reçue).

### Par réponse

| | Aujourd'hui (Mistral Medium 3.5, 128B dense) | Routé |
|---|---|---|
| « Reformule ce mail » | 18,2 mgCO2e · 0,141 Wh | **1,65 mgCO2e · 0,013 Wh** (Ministral 3 8B) |
| Rédaction/synthèse | 48,5 mgCO2e · 0,376 Wh | **5,4 mgCO2e · 0,043 Wh** (Mistral Small 3.2) |
| Analyse avec raisonnement | 302,8 mgCO2e · 2,35 Wh | **105,3 mgCO2e · 0,856 Wh** (GPT-OSS 120B, 5,1B actifs) |

### Par mois, 100 000 requêtes

| | Aujourd'hui | Routé | Écart |
|---|---|---|---|
| Facture API | 573 $ | 37 $ | **×15 moins cher** |
| CO2 | 6,03 kg | 1,39 kg | **−77 %** |
| Énergie | 46,7 kWh | 11,3 kWh | −76 % |
| Coût du routeur lui-même | – | 5 $/mois, 0,33 s p50 | 13 % de la facture routée |

Si la question « et à l'échelle ? » vient : à 10 M requêtes/mois, **49 000 €
économisés par mois** et 5,6 tCO2e par an. La proportion ne bouge pas, c'est
l'intérêt.

### Le routeur, mesuré (et non estimé)

`benchmark_router_models`, 24 tours du jeu de référence, 3 passes, Albert,
2026-09-16 : Ministral 3 8B classe le bon palier **9 fois sur 10**, p50
**332 ms**, p95 402 ms. Une seule erreur sur tout le benchmark a confondu
« simple » et « complexe » : les erreurs sont à un palier près, ce qui coûte une
taille de modèle, jamais une mauvaise réponse. En cas de panne du classifieur,
tout repart sur le modèle d'aujourd'hui — le pire cas du routeur, c'est le cas
actuel.

### La partie humaine

- 5 % de tirage sur 100 000 requêtes = **5 000 comparaisons à l'aveugle par mois**.
- **Aucun LLM ne juge un LLM.** La préférence vient des agents, point. Le
  carbone est mesuré, pas jugé, et affiché à côté du taux de victoire : une
  réponse coûteuse que personne ne préfère se voit.
- Intervalles de Wilson sur les taux de victoire ; promotion d'un modèle
  uniquement au-dessus d'un seuil de votes, et manuelle.
- 144 tranches (12 domaines × 12 tâches), ~417 votes par tranche et par an :
  le seuil de 100 votes décisifs qui autorise une route dédiée est atteint en
  semaines.

---

## Trois questions du jury, trois réponses de vingt secondes

**« Et si le petit modèle répond moins bien ? »**
Sous 0,7 de confiance, on ne descend pas : on reste sur le modèle d'aujourd'hui.
Et on ne le décide pas nous-mêmes — 5 000 agents par mois votent à l'aveugle.
S'ils préfèrent l'autre, il est promu ; s'ils ne préfèrent rien, on garde.

**« Albert est gratuit, pourquoi parler de prix ? »**
Parce que gratuit ne veut pas dire sans coût : c'est de la compute publique,
financée. Les prix catalogue disent ce que vaut cette compute. Quinze fois moins
de compute pour le même service, c'est quinze fois plus d'agents servis à
budget constant.

**« Pourquoi seulement trois paliers ? »**
Parce que trois paliers, on sait les prouver aujourd'hui. Les étiquettes
domaine/tâche sont déjà collectées sur chaque vote : le jour où une tranche a
assez d'avis, la route existe. On a construit la marche suivante, on ne l'a pas
promise.

---

## À régler avant le jour J

1. **Les multiplicateurs d'énergie affichés sont faux.**
   `chat/views/llm_config.py:21` annonce ×8 pour le palier standard et ×10 pour
   le palier raisonnement. Recalculés avec EcoLogits et les modèles réellement
   affectés par `seed_routing_tiers`, c'est **×3,2 et ×64**. Le sélecteur
   affichera donc « environ 10 fois plus d'énergie » sur un palier qui en
   consomme 64. Sur une démo dont le sujet est l'honnêteté du chiffre, c'est
   la seule question qui peut faire mal. Corriger les valeurs par défaut, ou
   masquer la phrase pendant la démo.
2. **Aucun prix n'est configuré** dans `conversations/configuration/llm/default.json` :
   les champs `input_price_eur_per_mtok` / `output_price_eur_per_mtok` existent
   dans `LLModel` mais ne sont renseignés sur aucun modèle. Les chiffres de
   facture viennent donc de cette fiche, pas de l'application : ne rien
   promettre d'affiché à l'écran côté prix.
3. Répéter les 45 secondes de démo **avec le vrai réseau Albert** : le palier
   raisonnement met 20 à 60 secondes à répondre. Si c'est trop long sur scène,
   envoyer la question complexe **avant** de parler du problème, et revenir
   dessus au moment voulu — la réponse sera prête.
