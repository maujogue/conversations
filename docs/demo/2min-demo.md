# Démo 2 minutes — le routeur, prouvé par l'arène

Public visé : un décideur public qui a signé pour « un assistant, un modèle ».

Thèse : garder un seul modèle pour tout n'est plus un arbitrage, c'est un
réflexe. Et ce n'est pas nous qui le disons — ce sont 5 000 agents par mois.

Structure : **décision → preuve.** Le routeur est une affirmation (« le petit
modèle suffit ») ; l'arène est ce qui rend cette affirmation réfutable. Ne
jamais présenter l'arène comme « et en plus, c'est évolutif » : c'est la
réponse à l'objection que le routeur soulève.

Format : ~300 mots dits, un présentateur, un second à l'écran. Une seule
conversation, aucune navigation.

---

## Le déroulé (2:00)

### 0:00 – 0:12 · L'accroche

Écran : une phrase, fond noir.

> « Traduis cette phrase en anglais. »
>
> Aujourd'hui, cette question part sur un modèle de 128 milliards de
> paramètres. **Onze fois** l'énergie nécessaire. Pas une fois : à chaque fois.

Pas de « bonjour, nous sommes l'équipe X ». Le jury a vu quatorze groupes : il
lui faut une accusation, pas un sommaire.

### 0:12 – 0:26 · Le problème, et l'envoi n°1 lancé tout de suite

Écran : les huit modèles Albert, un seul allumé. **Pendant qu'on parle, envoyer
la question complexe** — elle met 20 à 60 secondes, elle doit partir maintenant.

> Albert sert huit modèles. L'assistant en utilise un, pour tout : la
> reformulation d'un mail comme l'arbitrage d'un budget. Ni le moins cher, ni le
> plus intelligent : celui qui a été choisi au début, et que personne n'a
> rouvert depuis.
>
> Nous n'avons pas construit un modèle. Nous avons rouvert ce choix.

**Dire une fois, avant de basculer sur l'application :**
> « Regardez la ligne **au-dessus** de la réponse. C'est la seule chose qui
> compte. »

Sans cette phrase, le jury lit le texte de la réponse et rate la légende.

### 0:26 – 0:40 · Envoi n°1 : la question dure (déjà en cours)

Ce qu'on voit : **« Réflexion en cours… 23 s »**, puis **« Auto · Modèle de
raisonnement »**, 3 feuilles.

> La lenteur n'est pas un défaut, c'est la preuve : il raisonne vraiment. Et
> c'est exactement pour ça qu'on ne veut pas de ce modèle-là pour reformuler un
> mail.

Le temps d'attente est retourné en argument. Ne pas s'en excuser.

### 0:40 – 0:52 · Envoi n°2 : la question simple

Même conversation, message suivant. Réponse quasi instantanée, **« Auto ·
Modèle rapide »**, 1 feuille.

> Même fil, même utilisateur, rien de réglé. Le routeur reclasse **à chaque
> message** — et il l'affiche. L'agent n'a pas à choisir, mais il voit ce qui a
> été choisi pour lui.

### 0:52 – 1:30 · Envoi n°3 : l'arène, la preuve

Message suivant : une question de rédaction courante. Le tirage part
automatiquement (voir la configuration plus bas) : **deux réponses côte à côte,
aucun nom de modèle, aucune feuille.**

> Voilà ce qu'un agent voit 5 % du temps. Deux réponses à sa question. Il ne
> sait pas quels modèles, il ne sait pas lequel coûte quoi.

**Puis, au jury, en s'arrêtant :**
> Prenez dix secondes. Laquelle est la meilleure ?

Laisser le silence. Personne ne tranchera nettement — c'est le but.

> À gauche, le modèle que vous payez aujourd'hui : 128 milliards de paramètres.
> À droite, un modèle **cinq fois plus petit**. Vous ne les distinguez pas.
> **Nous non plus.** C'est pour ça que ce n'est ni vous ni nous qui décidons :
> ce sont 5 000 agents par mois, à l'aveugle.

Voter. La réponse choisie **glisse dans la conversation** et récupère sa
feuille — le coût n'apparaît qu'**après** la préférence, jamais avant. Puis la
carte de remerciement avec le compteur d'avis.

> Et quand une tranche a assez d'avis, un bouton dans l'administration remplace
> le modèle du palier. C'est comme ça que la facture passe de 573 à 37 dollars :
> pas par notre conviction, par leurs votes.

### 1:30 – 1:48 · Les chiffres

Écran : la fiche, un seul tableau.

> Sur 100 000 requêtes par mois : la facture passe de **573 $ à 37 $**, quinze
> fois moins. Le carbone de **6,0 à 1,4 kg**, moins 77 %. Le routeur se paie
> lui-même : 5 $ par mois, 0,3 seconde, et il classe juste 9 fois sur 10.

Phrase de bascule, lentement :

> **Continuer avec un seul modèle, ce n'est plus un arbitrage : c'est une
> habitude qui coûte quinze fois le prix.**

### 1:48 – 2:00 · La suite, et la chute

> Chaque vote est étiqueté : domaine et tâche. Douze domaines, douze tâches.
> On ne route pas encore dessus — on constitue le jeu de données. Dans un an,
> ce ne sont plus trois paliers, c'est une route par usage, décidée par les
> agents.
>
> Trois paliers aujourd'hui, parce que c'est ce qu'on sait prouver.

> **Le bon choix n'est pas le plus gros modèle. C'est celui que les agents
> préfèrent — et il se trouve qu'il est moins cher.**

---

## Les trois questions (testées, issues du jeu de référence)

À taper telles quelles. Elles viennent de `chat/router/gold_set.json`, donc leur
palier est celui sur lequel le classifieur a été mesuré.

**1. Complexe** — envoyée en premier, pendant l'accroche :
> Compare trois scénarios de financement d'une cantine scolaire sur cinq ans,
> calcule le coût total actualisé à 3 % et recommande-en un.

*(finance / reasoning → palier `complex` → GPT-OSS 120B, effort `high`)*

**2. Simple** :
> Reformule ce mail pour un directeur : merci de votre retour rapide.

*(communication / writing → palier `simple` → Ministral 3 8B)*

**3. Standard** — celle qui déclenche l'arène :
> Rédige un courrier de refus motivé à une demande de subvention associative.

*(administrative / writing → palier `standard`)* Choisie exprès : deux lettres
administratives plausibles sont **indiscernables** pour un jury. C'est ce qui
fait marcher la question « laquelle est la meilleure ? ».

---

## Configuration à faire avant (à vérifier le matin même)

### 1. Drapeaux

`arena` = ENABLED, `router` = ENABLED, `arena_manual` = ENABLED (secours),
`dev_model_picker` = DISABLED (aucun nom de modèle ne doit pouvoir s'afficher).

### 2. Paliers

```bash
python manage.py seed_routing_tiers
```

Puis, dans l'admin, **modifier le palier standard** :

| Champ | Valeur de démo | Pourquoi |
|---|---|---|
| `standard_model_hrid` | `mistral-medium-3-5` | Le champion de l'arène doit être **le modèle d'aujourd'hui**. C'est le statu quo qu'on met en cause. |
| `standard_alternatives` | `mistral-small-3-2`, `gemma-4-31b` | Les challengers de ce palier. |

Laisser `simple` = `ministral-3-8b` et `complex` = `gpt-oss-120b`.

> **À assumer si on pose la question :** dans cette configuration, le palier
> standard tourne encore sur le modèle actuel — les 573 $ → 37 $ de la fiche
> correspondent à l'*après-promotion*, quand l'arène aura fait passer le palier
> standard sur Mistral Small. C'est précisément ce que la démo montre : le
> mécanisme qui produit le chiffre de la diapositive. Le dire avant qu'on le
> demande, ça renforce.

### 3. L'expérience d'arène

Nouvelle `ArenaExperiment` :

| Champ | Valeur |
|---|---|
| `tier` | `standard` |
| `is_active` | ✅ (désactiver toute autre expérience active sur ce palier) |
| `sampling_rate` | **1.0** |
| `daily_cap_per_user` | 50 (répétitions) |
| challenger | `mistral-small-3-2`, `is_control` **décoché** |

**Ne jamais compter sur un tirage à 5 % sur scène.** Le tirage est échantillonné
([arena.py:317](../../src/backend/chat/arena.py:317)) et plafonné par
utilisateur : à 5 %, on envoie quatre messages devant le jury et il ne se passe
rien. `sampling_rate = 1.0` rend le troisième envoi certain.

Deux validations à connaître, elles refusent l'enregistrement sinon :
- le challenger doit avoir **exactement la même liste d'outils** que le champion
  ([models.py:609](../../src/backend/chat/models.py:609)) — `mistral-small-3-2`
  et `mistral-medium-3-5` sont identiques sur ce point, ça passe ;
- une seule expérience active par palier.

Bonne nouvelle : avec le routeur activé, l'éligibilité au tirage se juge **par
tour** (`routing_decision.model_hrid == champion_hrid`), pas au niveau de la
conversation. Les deux premiers envois sur d'autres paliers ne bloquent donc pas
le tirage du troisième dans le même fil.

### 4. Secours

Si le tirage ne part pas : le bouton **« Essayer une autre réponse »**
(`arena_manual`) sur la dernière réponse produit la même comparaison à la
demande. Attention, il tire dans les **alternatives du palier**
([arena.py:425](../../src/backend/chat/arena.py:425)) — donc `mistral-small-3-2`
ou `gemma-4-31b`, jamais un challenger de contrôle. Garder une seule alternative
configurée pendant la démo si on veut être sûr de qui répond.

Et garder une **capture vidéo des 70 secondes de démo** : c'est le seul vrai
risque de la présentation.

---

## Fiche chiffres

Hypothèses à dire à voix haute si on les demande : 100 000 requêtes/mois,
répartition 45 % simple / 45 % standard / 10 % complexe, réponses types de
150 / 400 / 500+2 000 jetons (spec §10), prix publics catalogue (Albert facture
zéro : ce chiffre dit ce que **vaut** la compute publique, pas une facture reçue),
CO2 calculé avec EcoLogits, même appel que `chat/footprint.py`.

### Par réponse

| | Aujourd'hui (Mistral Medium 3.5, 128B dense) | Routé |
|---|---|---|
| « Reformule ce mail » | 18,2 mgCO2e · 0,141 Wh | **1,65 mgCO2e · 0,013 Wh** (Ministral 3 8B) |
| Rédaction / synthèse | 48,5 mgCO2e · 0,376 Wh | **5,4 mgCO2e · 0,043 Wh** (Mistral Small 3.2) |
| Analyse avec raisonnement | 302,8 mgCO2e · 2,35 Wh | **105,3 mgCO2e · 0,856 Wh** (GPT-OSS 120B, 5,1B actifs) |

### Par mois, 100 000 requêtes

| | Aujourd'hui | Routé | Écart |
|---|---|---|---|
| Facture API | 573 $ | 37 $ | **×15 moins cher** |
| CO2 | 6,03 kg | 1,39 kg | **−77 %** |
| Énergie | 46,7 kWh | 11,3 kWh | −76 % |
| Le routeur lui-même | – | 5 $/mois, 0,33 s p50 | 13 % de la facture routée |

À l'échelle : 10 M requêtes/mois → **49 000 € économisés par mois**, 5,6 tCO2e
par an. La proportion ne bouge pas, c'est tout l'intérêt.

### Le routeur, mesuré et non estimé

`benchmark_router_models`, 24 tours du jeu de référence, 3 passes, Albert,
2026-09-16 : Ministral 3 8B trouve le bon palier **9 fois sur 10**, p50
**332 ms**, p95 402 ms. Une seule erreur sur tout le benchmark a confondu
« simple » et « complexe » : les erreurs sont à un palier près, ce qui coûte une
taille de modèle, jamais une mauvaise réponse. Classifieur en panne → tout
repart sur le modèle d'aujourd'hui : **le pire cas du routeur, c'est la
situation actuelle.**

### La partie humaine

- 5 % de tirage sur 100 000 requêtes = **5 000 comparaisons à l'aveugle par mois**,
  60 000 avis par an.
- **Aucun LLM ne juge un LLM.** La préférence vient des agents. Le carbone est
  mesuré, pas jugé, et affiché à côté du taux de victoire : une réponse coûteuse
  que personne ne préfère se voit.
- Intervalles de Wilson sur les taux de victoire ; promotion manuelle, au-dessus
  d'un seuil de votes.
- 144 tranches (12 domaines × 12 tâches), ~417 votes par tranche et par an : le
  seuil de 100 votes décisifs qui autorise une route dédiée est atteint en
  semaines.

---

## Trois questions du jury, trois réponses de vingt secondes

**« Et si le petit modèle répond moins bien ? »**
Sous 0,7 de confiance, on ne descend pas : on reste sur le modèle d'aujourd'hui.
Et ce n'est pas nous qui tranchons — 5 000 agents par mois votent à l'aveugle.
S'ils préfèrent l'autre, il est promu ; s'ils ne préfèrent rien, on garde.

**« Albert est gratuit, pourquoi parler de prix ? »**
Gratuit ne veut pas dire sans coût : c'est de la compute publique, financée. Les
prix catalogue disent ce qu'elle vaut. Quinze fois moins de compute pour le même
service, c'est quinze fois plus d'agents servis à budget constant.

**« Pourquoi seulement trois paliers ? »**
Parce que trois paliers, on sait les prouver. Les étiquettes domaine/tâche sont
déjà collectées sur chaque vote : le jour où une tranche a assez d'avis, la
route existe. On a construit la marche suivante, on ne l'a pas promise.

---

## À régler avant le jour J

1. **Les multiplicateurs d'énergie affichés sont faux.**
   [`views/llm_config.py:21`](../../src/backend/chat/views/llm_config.py:21)
   annonce ×8 pour le palier standard et ×10 pour le palier raisonnement.
   Recalculés avec EcoLogits et les modèles réellement affectés, c'est **×3,2 et
   ×64**. Le sélecteur affichera donc « environ 10 fois plus d'énergie » sur un
   palier qui en consomme 64. Sur une démo dont le sujet est l'honnêteté du
   chiffre, c'est la seule question qui peut faire mal. Corriger les valeurs par
   défaut, ou ne pas ouvrir le sélecteur pendant la démo.
2. **Aucun prix n'est configuré** dans `configuration/llm/default.json` : les
   champs `input_price_eur_per_mtok` / `output_price_eur_per_mtok` existent sur
   `LLModel` mais ne sont renseignés sur aucun modèle. Les chiffres de facture
   viennent de cette fiche, pas de l'application : ne rien promettre d'affiché à
   l'écran côté prix.
3. **Répéter avec le vrai réseau Albert**, en chronométrant l'envoi n°1 : tout
   le déroulé suppose qu'il revient avant 0:40. S'il met 60 secondes, l'envoyer
   dès la diapositive d'accroche.
4. **Vérifier que la comparaison tient la route** : lancer la question n°3 cinq
   fois en répétition et regarder les deux réponses. Si Mistral Small perd
   visiblement, changer de question plutôt que de parier. (Si ça arrive quand
   même sur scène, la ligne honnête existe : « et c'est exactement pour ça qu'on
   ne décide pas nous-mêmes » — mais ne pas compter dessus.)
