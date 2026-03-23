# RL Environment Assignment
### Romanian Crossword Generator as an RL Training Environment

---

## Question 1: Describe the Environment. What Makes It Interesting?

**The Environment:**

The LLM must generate a complete, valid Romanian barred crossword puzzle from scratch — starting from nothing but a grid size and a theme — using only LLM-based reasoning. No backtracking algorithms, no constraint solvers. The LLM must figure out how to place words, manage clue cells, resolve letter intersections, and produce a solvable puzzle purely through structured thinking and iterative self-correction.

This is actually the central question I keep running into while building this project: **can an LLM replace a classical combinatorial algorithm for a problem that classical algorithms were specifically designed to solve?** Turning that question into an RL environment felt like a natural fit.

**What makes it genuinely interesting:**

**1. It hits a known LLM weakness head-on.**
LLMs are notoriously poor at letter-level reasoning. Crossword generation forces the LLM to reason about individual letter positions across multiple intersecting words simultaneously — that's exactly where LLMs struggle, which makes the difficulty real rather than artificially constructed.

**2. The constraint space is genuinely hard.**
A valid Romanian barred crossword requires satisfying many structural rules all at once — word validity, clue cell placement, intersection consistency, no duplicates, and more. A classical algorithm handles this through search and pruning. An LLM has to handle it through reasoning and self-correction. Whether it can actually do that — and how it improves over RL training — is what makes this environment worth building.

**3. The reward gradient is smooth and meaningful.**
The LLM won't solve this perfectly on the first attempt and that's a feature, not a bug. It might get word placement mostly right but violate clue adjacency rules, scoring around 0.4. Then it learns to fix that but produces duplicate words, reaching 0.55. Each improvement is measurable and rewarded incrementally — exactly the kind of signal RL training needs. A binary pass/fail task would produce no useful gradient at all.

**4. It comes from something I'm actually building.**
This isn't a synthetic benchmark designed to sound impressive. I've been working on a Romanian crossword generator and the failure modes here are ones I've personally run into. The rules reflect real product constraints, the edge cases are known, and there are no accidental shortcuts hiding in the problem.

---

## Question 2: Tools, Packages, and Data

**Data provided to the LLM:**

```
/data/rules.md                  ← full crossword structural rules
/data/grid_schema.json          ← expected JSON schema for the output
/data/example_small.json        ← complete valid crossword, 7×7 grid
/data/example_medium.json       ← complete valid crossword, 9×9 grid
/data/example_large.json        ← complete valid crossword, 12×12 grid
                                   (all three on different themes)
```

No word database is provided. The LLM must rely entirely on its own internal knowledge of Romanian vocabulary to place words — which is intentional. Providing a wordlist would undermine the core premise of the environment: we want to know whether an LLM can generate a valid crossword through reasoning alone, not whether it can query a dictionary. The judge uses a hidden reference wordlist to validate outputs, but the LLM never sees it.

Three example grids are provided instead of one, specifically across different sizes and themes. One example teaches the LLM the format. Multiple examples teach it what valid structure looks like across different grid shapes — particularly how CLUE_BOX placement patterns scale, how intersection density changes with grid size, and how the top-left rule manifests in practice. The LLM is instructed to study the structural patterns across all three examples, not copy their content.

No seeded grid is provided. The LLM starts from a completely blank canvas.

**Python packages:**

The question asks what the LLM needs inside the sandbox environment. It is worth separating this from the infrastructure layer, which the LLM never directly touches.

*Inside the sandbox (what the LLM uses):*

```
google-adk    ← manages the agent loop — tool routing, conversation state,
                 and episode lifecycle across multiple turns
jsonschema    ← output format validation
loguru        ← structured logging
```

ADK is the only GCP package the LLM needs directly. It handles tool calls to `validate_grid()` and `submit()`, maintains conversation history across turns, and enforces the API call budget. The LLM interacts with Gemini through ADK — it does not need to import a separate LLM SDK.

```python
from google.adk.agents import Agent
from google.adk.tools import FunctionTool

agent = Agent(
    model=gemini-2.0-flash,
    name=crossword_generator,
    instruction=CROSSWORD_PROMPT,
    tools=[FunctionTool(validate_grid), FunctionTool(submit)]
)
```

*Infrastructure layer (outside the sandbox, not used by the LLM directly):*

```
google-genai              ← Gen AI SDK used by the ADK runtime internally
google-cloud-aiplatform   ← submitting the PPO training job to Vertex AI
google-cloud-pubsub       ← reward stream between judge and training job
trl                       ← PPO training logic inside the Vertex AI container
```

**Tools — kept minimal and deliberate:**

```python
def validate_grid(grid: dict) -> dict:
    """
    Run full rule validation against the current grid state.
    Returns {"valid": bool, "violations": [...]}
    The LLM should call this frequently during generation, not just at the end.
    """

def submit(grid: dict) -> dict:
    """Submit the completed grid for final scoring."""
```

Only two tools. No word lookup, no pattern search. The LLM has access to rule validation so it can self-correct, and a submit function to trigger scoring. Everything else — vocabulary, word placement decisions, intersection reasoning — must come from the LLM itself.

**GPU/TPU requirements:**

The episode generation itself doesn't require a GPU since the LLM reasoning happens via API calls to hosted models like Gemini or Claude. GPUs become relevant in two places. First, if you want to run a smaller open-source model locally inside the sandbox rather than calling an external API — which gives more control over the training loop and avoids API costs at scale across thousands of parallel episodes. Second, and more importantly, the Vertex AI PPO training job that updates the LLM weights based on accumulated rewards is computationally intensive and runs on A100s provisioned through Vertex AI. Episode generation and weight updates are deliberately separate workloads — episodes are API-light and highly parallelizable, while the PPO update step is GPU-heavy and runs in batches once enough rewards have accumulated via Pub/Sub.

**Hidden from the LLM:**

```
/judge/held_out_themes/        ← unseen themes for generalization testing
/judge/reference_wordlist.tsv  ← Romanian word database used only by the judge
/judge/reference_validator.py  ← authoritative rule checker
```

---

## Question 3: The Prompt

```
You are an AI engineer building a Romanian barred crossword puzzle generator.
Your task is to generate a complete, valid Romanian crossword from scratch
using LLM-based reasoning.

IMPORTANT: You must NOT use classical search algorithms such as backtracking,
constraint propagation, or OR-Tools solvers. The crossword must be built
through LLM reasoning — using language model calls to make placement
decisions, resolve conflicts, and generate clues.

BACKGROUND
----------
A Romanian barred crossword embeds clues directly inside the grid as
CLUE_BOX cells, not in a separate list. Each CLUE_BOX licenses one or
more adjacent word starts with an implied direction. Players fill in
LETTER cells guided by clues embedded in the grid itself.

CELL TYPES
----------
  EMPTY_PLAYABLE  → blank, not yet assigned
  LETTER          → contains one letter, part of one or two intersecting words
  CLUE_BOX        → blocked cell that licenses adjacent word starts
  BLOCKER_ZONE    → optional decorative non-playable region

TASK
----
Generate a complete Romanian crossword puzzle on this theme:

    Theme: "NATURĂ" (Nature)
    Grid size: 9 rows × 9 columns

Your generation process should follow these steps:
  1. Study the three example grids in /data/ — focus on structural patterns,
     not content. Notice how CLUE_BOX cells are placed, how intersections
     work, and how the layout scales across different grid sizes.
  2. Generate 10-15 theme-related Romanian words with clues using LLM calls
  3. Design the grid layout — decide where CLUE_BOX and LETTER cells go
  4. Place theme words first, then fill remaining slots with Romanian words
     from your own knowledge of the language
  5. Generate Romanian clues for all placed words
  6. Validate using validate_grid() and fix any violations before submitting

KEY RULES (see /data/rules.md for the full specification):
  - Every word of length ≥3 must be a valid Romanian word
  - Every word start must be licensed by an adjacent CLUE_BOX in the
    correct direction (see rules.md for allowed adjacency per direction)
  - No two CLUE_BOX cells may be orthogonally adjacent
  - One CLUE_BOX may license at most 3 word starts
  - No duplicate words anywhere in the grid
  - Intersecting words must share the same letter at every crossing cell
  - Top-left cell must always be a CLUE_BOX
  - No CLUE_BOX in the bottom-right 2×2 corner
  - All clues must be in Romanian, max 4 words, no punctuation except ... or !

DELIVERABLES
------------
  /solution/crossword.json       ← completed grid matching /data/grid_schema.json
  /solution/wordlist.json        ← [{word, clue, direction, row, col}, ...]
  /solution/generation_log.txt   ← log of your reasoning and decisions

Then call submit() with your completed grid.

TOOLS
-----
  validate_grid(grid)            → violations with exact cell locations
  submit(grid)                   → submit for final scoring

GUIDANCE
--------
Call validate_grid() after placing each word, not just at the end.
If it returns violations, reason through how to fix them before continuing.
A grid that is 80% filled and self-corrected scores better than one that
is 100% filled but ignores rule violations.

You have a budget of 100 LLM API calls. Use them thoughtfully.
```

---

## Question 4: The Judge

The judge is a fully isolated Cloud Function — the LLM cannot read, predict, or influence its behavior in any way.

**The judge performs the following steps:**

**(1) File existence check.**
Verify that `/solution/crossword.json`, `/solution/wordlist.json`, and `/solution/generation_log.txt` all exist. If any are missing, fail with score 0.

**(2) Forbidden algorithm check.**
Parse all `.py` files in `/solution/` using `ast.parse`. Scan for imports or usage of `ortools`, `python-constraint`, `backtrack`, or any recursive function that takes a grid as an argument. If found, fail with score 0. The whole point of this environment is LLM-driven generation — a classical algorithm is the wrong answer regardless of whether it produces a valid grid.

**(3) Output schema check.**
Validate `/solution/crossword.json` against `/data/grid_schema.json`. Every cell must have a valid `type`. `LETTER` cells must have a `letter` field. `CLUE_BOX` cells must have a `clues_hosted` list. If malformed, fail with score 0.

**(4) Intersection consistency check.**
For every cell shared between an across word and a down word, verify both words agree on the letter at that cell. This is the most fundamental correctness check — a randomly filled grid scores near 0%, a correctly solved grid scores 100%.

**(5) Word validity check.**
For every letter sequence of length ≥3, look up the normalized form in `romanian_wordlist.tsv`. Compute the fraction that are valid Romanian words, scored linearly between 0.0 and 1.0.

**(6) Structural rules check.**
Run the reference validator against the submitted grid, checking each rule independently:

```python
rules = {
    "every_word_start_licensed":        0.20,
    "no_adjacent_clue_boxes":           0.15,
    "max_3_licenses_per_clue_box":      0.10,
    "no_duplicate_words":               0.10,
    "word_boundaries_respected":        0.10,
    "top_left_is_clue_box":             0.05,
    "no_clue_box_bottom_right_corner":  0.05,
    "grid_fully_filled":                0.05,
}
```

**(7) Clue quality check.**
For each word in `/solution/wordlist.json`, verify the clue is in Romanian, contains at most 4 words, and uses no punctuation except `...` or `!`. Score based on the fraction of clues passing all three checks.

**(8) Held-out theme generalization test.**
Run the LLM's generation approach on 2 unseen themes from `/judge/held_out_themes/`. Apply steps 4–7 to each result. This is the most important anti-hardcoding check — an approach that only works for "NATURĂ" is not a generator, it is a memorized answer.

**(9) Reasoning quality check.**
Read `/solution/generation_log.txt`. Verify that `validate_grid()` was called at least 3 times and that the log shows the LLM actually responding to violations rather than ignoring them. A log with zero self-correction is penalized — it suggests the grid was assembled without genuine iterative reasoning.

**(10) Final score:**

```python
score = (
    0.25 * intersection_consistency   # letters match at all crossings
  + 0.20 * structural_rules_score     # layout rules respected
  + 0.20 * word_validity_score        # real Romanian words
  + 0.20 * held_out_generalization    # works on new themes
  + 0.10 * clue_quality_score         # valid Romanian clues
  + 0.05 * reasoning_quality_score    # evidence of self-correction
)

# Pass threshold: 0.70
```

**The LLM fails if it:** uses a classical algorithm, produces intersecting words with conflicting letters, places words not in the Romanian wordlist, only works for the training theme, or never self-corrects using `validate_grid()`.

**The LLM succeeds if it:** designs a coherent grid layout through LLM judgment, places Romanian words that correctly share letters at intersections, respects all clue cell placement rules, generates grammatically correct Romanian clues, and generalizes its approach to themes it has never seen.

---

## Question 5: Reward Hacking and Reward Denial?

### Reward Hacking risks

**1. Hardcoding a valid grid for the "NATURĂ" theme.**
The LLM could construct one valid crossword during development and return it directly when it recognizes the training theme, skipping any real generation logic.

*Fix:* The held-out theme test (weight 0.20) makes this mathematically unworkable. A hardcoded single-theme solution scores 0 on unseen themes, capping the total at around 0.50 — well below the 0.70 pass threshold.

**2. Faking the generation log.**
The LLM could write a convincing `generation_log.txt` describing LLM calls that never actually happened, then submit a prebuilt valid grid.

*Fix:* The judge cross-references the log against Cloud Run's outbound HTTP request logs. If the log claims 40 LLM calls but the sandbox recorded only 3 actual requests to the Gemini API, the log is fabricated and the reasoning score drops to 0.

**3. Wrapping a classical algorithm in fake LLM calls.**
The LLM could implement backtracking, make one LLM call at the start to generate a word list, then run a classical solver for the actual fill — technically making LLM calls while bypassing LLM reasoning for the hard part.

*Fix:* The forbidden algorithm check catches structural backtracking patterns in the code. The reasoning quality check additionally looks for evidence that LLM responses influenced placement decisions at multiple points throughout generation — not just at initialization.

### Reward Denial risks

**1. Valid words rejected due to diacritic normalization mismatch.**
A correct Romanian word with diacritics fails the database lookup because the wordlist stores normalized forms without diacritics.

*Fix:* The judge applies the same normalization (`ăâîșț → aaist`) before all lookups. The word `PĂDURE` normalizes to `PADURE` in both the LLM's output and the judge's lookup — they always match.

**2. Creative but valid Romanian clues penalized.**
The LLM might write a perfectly good clue that doesn't match any reference string. A strict content comparison would unfairly penalize original but correct clues.

*Fix:* The clue check validates structure only — length, punctuation, language detection. It never compares against a reference answer. Any valid Romanian clue of ≤4 words passes.

**3. Near-valid grids punished as harshly as completely wrong ones.**
A grid with one clue adjacency violation out of 30 word starts shouldn't score the same as a grid with 30 violations.

*Fix:* Every rule is scored as a fraction, not a binary pass/fail. One violation out of 30 scores 29/30 = 0.97, preserving the smooth reward gradient that RL depends on.

---

## Question 6: Why This Environment? How Does It Connect to My Experience?

I'm building a Romanian barred crossword generator as a personal project and the question of whether LLM reasoning can replace classical constraint-solving is something I think about constantly while working on it. Every time I implement a new part of the system I run into the same tension — the classical algorithm is reliable but rigid, the LLM is flexible but unpredictable. Turning that tension into an RL environment felt like the most honest way to answer the question systematically.

What I find most compelling about this as an RL environment is that it tests something that genuinely matters beyond crosswords. If an LLM can learn to generate valid constrained structures through iterative reasoning — getting better over thousands of training episodes — that's evidence of a kind of structured thinking that would be valuable across a wide range of AI engineering problems.

Romanian as the target language adds a layer of difficulty that isn't artificial. It's a low-resource language with rich morphology, real diacritic complexity, and far fewer available NLP tools than English. The LLM can't lean on pattern matching from training data as heavily as it could for English crosswords — it actually has to reason about the constraints.

---

## GCP Architecture

The environment runs entirely on GCP, with each service having a specific non-overlapping role.

```
┌──────────────────────────────────────────────────────────────────┐
│                    RL Training System on GCP                      │
│                                                                  │
│  ┌─────────────────┐   tools    ┌──────────────────────────┐    │
│  │   ADK Agent     │◄──────────►│   Cloud Run Sandbox      │    │
│  │                 │            │   - LLM generates puzzle  │    │
│  │  manages the    │            │   - tools available       │    │
│  │  tool call loop │            │   - no internet access    │    │
│  └───────┬─────────┘            └─────────────┬────────────┘    │
│          │                                     │ submit()         │
│          │ reward                              ▼                  │
│          │                      ┌──────────────────────────┐     │
│          └──────────────────────│  Cloud Function (Judge)  │     │
│                                 │  - fully isolated         │     │
│                                 │  - runs all 9 steps       │     │
│                                 │  - returns score 0–1      │     │
│                                 └──────────────┬────────────┘    │
│                                                │                  │
│                                ┌───────────────┴──────────┐      │
│                                ▼                          ▼       │
│                     ┌──────────────────┐   ┌─────────────────┐   │
│                     │     Pub/Sub      │   │    BigQuery     │   │
│                     │  reward stream   │   │  episode logs   │   │
│                     └────────┬─────────┘   └─────────────────┘   │
│                              ▼                                    │
│                     ┌──────────────────┐                         │
│                     │   Vertex AI      │                         │
│                     │  PPO Training    │                         │
│                     └──────────────────┘                         │
└──────────────────────────────────────────────────────────────────┘
```

**Google ADK** manages the agent loop — the LLM calls tools across multiple turns, reasons about violations, self-corrects, and eventually calls `submit()`. ADK handles tool routing and conversation state natively, removing the need to build that infrastructure manually.

**Cloud Run** is the sandbox — a disposable, network-isolated container per episode. Each run starts fresh with no state leakage between episodes. It also logs all outbound HTTP requests, which lets the judge verify that real LLM API calls were made, closing the fake-log reward hacking vector described above.

**Cloud Functions** hosts the judge in complete isolation from the sandbox. The LLM cannot read, modify, or predict the judge's behavior. It submits its grid and gets a score back — nothing else.

**Pub/Sub** decouples reward from training so multiple episodes can run in parallel. Each episode publishes its score when done. The Vertex AI training job consumes rewards asynchronously — no sequential bottlenecks, high episode throughput.

**Vertex AI** handles the PPO weight updates. Rewards flow in from Pub/Sub, the LLM gets incrementally better at generating crosswords, and the next batch of episodes begins. Checkpoint management, GPU scaling, and experiment tracking all come included.

**BigQuery** logs every episode so I can track whether training is actually working:

```sql
SELECT
  FLOOR(episode / 100) * 100     AS episode_bucket,
  AVG(score)                      AS mean_reward,
  AVG(intersection_consistency)   AS letter_accuracy,
  AVG(word_validity)              AS word_validity,
  AVG(held_out_score)             AS generalization
FROM `project.rl_logs.episodes`
GROUP BY episode_bucket
ORDER BY episode_bucket
```

If `mean_reward` trends upward over time and `generalization` tracks with it, the LLM is genuinely learning to generate crosswords rather than memorizing specific themes. That's the result I'm looking for — and having it queryable in BigQuery means I can see it clearly rather than guessing.

---

## How the Key GCP Components Work

### Google ADK Agent — "The LLM doing the work"

ADK (Agent Development Kit) is a framework that lets an LLM use tools in a loop across multiple turns. Think of it as the "body" the LLM lives in during each episode — it manages conversation history, routes tool calls to the right functions, and keeps the loop running until the LLM decides it's done.

Here's what one episode looks like from ADK's perspective:

```
ADK starts a session
  ↓
Sends the prompt to the LLM: "Generate a Romanian crossword on theme NATURĂ"
  ↓
LLM responds: "I'll start by finding some nature-related words"
  + tool call: find_words_by_pattern("_____")
  ↓
ADK executes the tool, returns results to LLM
  ↓
LLM responds: "Now I'll place PADURE in row 2..."
  + tool call: validate_grid(current_grid)
  ↓
ADK executes, returns violations
  ↓
LLM responds: "I see a clue box conflict, let me fix it..."
  + tool call: validate_grid(fixed_grid)
  ↓
... this loop continues until the LLM is satisfied ...
  ↓
LLM responds: "Grid looks complete"
  + tool call: submit(final_grid)
  ↓
ADK ends the session, reward score is returned
```

Without ADK you'd have to build all of this yourself — managing conversation history across turns, routing tool calls, handling errors, enforcing the API call budget. ADK gives you all of that out of the box so you can focus on the environment design rather than the plumbing.

In code, one full episode looks roughly like this:

```python
from google.adk.agents import Agent
from google.adk.tools import FunctionTool

agent = Agent(
    model="gemini-2.0-flash",
    name="crossword_generator",
    instruction=CROSSWORD_PROMPT,
    tools=[
        FunctionTool(lookup_word),
        FunctionTool(find_words_by_pattern),
        FunctionTool(validate_grid),
        FunctionTool(submit),
    ]
)

# This runs one full episode — the LLM loops until it calls submit()
result = agent.run("Generate the crossword.")
```

---

### Pub/Sub Reward Stream — "The messenger between episodes and training"

Pub/Sub is a messaging system built on a simple principle: some services **publish** messages to a topic, and other services **subscribe** to receive them. In this system, the judge publishes reward scores and the Vertex AI training job subscribes to consume them.

The flow after each episode completes:

```
Episode finishes
  ↓
Judge scores the grid → reward = 0.73
  ↓
Judge PUBLISHES this message to a Pub/Sub topic:
  {
    "episode_id": "ep_4821",
    "reward": 0.73,
    "breakdown": {
      "intersection_consistency": 0.90,
      "word_validity": 0.85,
      "structural_rules": 0.60,
      "held_out_score": 0.55
    },
    "llm_actions": [...],
    "timestamp": "2026-03-14T10:23:01Z"
  }
  ↓
Vertex AI training job is SUBSCRIBED to that topic
  ↓
It receives the message and uses the reward to update the LLM weights
```

The key reason this matters is **parallelism**. Without Pub/Sub, training would be fully sequential:

```
Episode 1 runs → waits → judge scores → training updates → Episode 2 runs...
```

With Pub/Sub, many episodes run at the same time and rewards flow in as they complete:

```
Episode 1 runs ──────────────────────────────► publishes reward
Episode 2 runs ───────────────────► publishes reward
Episode 3 runs ────────────────────────────────────► publishes reward
Episode 4 runs ──────────────► publishes reward

Vertex AI training job consumes all rewards as they arrive,
batches them, and runs PPO updates continuously
```

You can run 20 episodes simultaneously and the training job processes all their rewards as they come in, rather than waiting for each one to finish before starting the next. This dramatically increases training speed.

---

### Vertex AI PPO Training — "How the LLM actually gets better"

PPO (Proximal Policy Optimization) is the algorithm that updates the LLM's weights based on accumulated rewards. This is where the actual learning happens. The intuition is simple: **actions that led to high rewards become more likely next time, actions that led to low rewards become less likely.**

Here's what one PPO update cycle looks like in this environment:

```
A batch of completed episodes arrives via Pub/Sub:

  Episode 4821: reward 0.73
    → LLM called validate_grid() 5 times, fixed 3 violations before submitting

  Episode 4822: reward 0.31
    → LLM never called validate_grid(), grid had 12 violations on submit

  Episode 4823: reward 0.81
    → LLM used find_words_by_pattern() effectively at every intersection

PPO looks at what actions the LLM took in high vs low reward episodes:

  High reward episodes:  called validate_grid() often
                         used find_words_by_pattern() for intersections
                         fixed violations when found before continuing

  Low reward episodes:   ignored validate_grid() output
                         placed words without checking patterns
                         submitted too early without self-correction

PPO update:
  ↑ increase probability of: calling validate_grid() after each placement
  ↑ increase probability of: using find_words_by_pattern() for intersections
  ↓ decrease probability of: submitting without self-correction
  ↓ decrease probability of: placing words without checking word validity
```

The "Proximal" part of PPO means it makes careful, conservative updates — it never swings the weights too far in any direction based on a single batch. This stability is why PPO became the standard algorithm for RL training of LLMs, including in the training of ChatGPT and Claude.

GCP does not provide a managed PPO trainer out of the box. In practice, the PPO logic is written using the open source `trl` library, packaged inside a Docker container, and submitted to Vertex AI as a custom training job. Vertex AI handles GPU provisioning, checkpointing, and job orchestration — the PPO logic itself runs inside the container.

Submitting the job to Vertex AI looks like this:

```python
from google.cloud import aiplatform

# PPO training code runs inside this container image
job = aiplatform.CustomContainerTrainingJob(
    display_name="crossword-ppo-training",
    container_uri="gcr.io/my-project/ppo-trainer:latest",
)

job.run(
    machine_type="n1-standard-8",
    accelerator_type="NVIDIA_TESLA_A100",
    accelerator_count=1,
)
```

Inside that container, the PPO logic uses `trl` — a real, widely used open source library for RL training of LLMs:

```python
from trl import PPOTrainer, PPOConfig   # open source, actively maintained
from google.cloud import pubsub_v1

# Pull completed episode rewards from Pub/Sub
def pull_rewards_from_pubsub():
    subscriber = pubsub_v1.SubscriberClient()
    response = subscriber.pull(subscription=SUBSCRIPTION_PATH, max_messages=32)
    rewards = []
    for msg in response.received_messages:
        episode = json.loads(msg.message.data)
        rewards.append(episode["reward"])
    return rewards

# Configure and run PPO updates
config = PPOConfig(
    learning_rate=1e-5,
    batch_size=32,
    ppo_epochs=4,
    cliprange=0.2,        # how conservative each update is
)

trainer = PPOTrainer(config=config, model=your_llm)

# Training loop — pulls rewards from Pub/Sub, updates LLM weights
for update_step in range(num_update_steps):
    rewards = pull_rewards_from_pubsub()
    trainer.step(queries, responses, rewards)
```

---

### How All Three Connect — The Full Training Loop

Putting it all together, one complete training cycle looks like this:

```
1. ADK Agent runs an episode
   LLM uses tools, generates crossword, eventually calls submit()
                     │
                     ▼
2. Judge scores the grid (0.0 – 1.0)
   Checks intersections, word validity, structural rules, generalization
                     │
                     ▼
3. Pub/Sub publishes the reward + episode data
   Non-blocking — the next episode can start immediately in parallel
                     │
                     ▼
4. Vertex AI PPO training job receives the reward
   Batches rewards from many parallel episodes
   Updates LLM weights — good actions become more likely
                     │
                     ▼
5. Improved LLM runs the next episode
   Loop repeats thousands of times
```

After enough episodes, the learning curve visible in BigQuery should show the LLM progressively improving — calling `validate_grid()` more often, using `find_words_by_pattern()` more strategically, and producing grids with fewer intersection conflicts. That trajectory from broken grids to valid puzzles, driven entirely by reward signal, is what the whole system is designed to produce.
