“Portal” describes what it does, but GHOSTroom describes what it becomes: the place inside GHOSTpad where you, Blender, and whichever agents you choose actually work together. It also scales naturally into collaboration and learning. You can “open GHOSTroom,” invite another agent into the room, switch who is leading, review the room history, enter Learn mode, etc. It feels more like a product than a UI component.

So I’d define the family as:

GHOSTpad = the Blender-on-iPad product
GhostBlender = the Blender control/tool layer
GhostBlender Simple = the external direct-access bridge
GHOSTroom = the native AI collaboration workspace inside GHOSTpad

And I’d formalise the target like this.

GHOSTpad Evolution Target

Product objective: Evolve GHOSTpad from a successful Blender-on-iPad port into an AI-native creative workstation in which humans and multiple AI agents can inspect, manipulate, discuss, review, teach and learn from the same live Blender workspace through one shared control architecture.

1. Preserve full Blender capability

GHOSTpad must remain Blender, not become a simplified modelling application wrapped around Blender. Existing Blender functionality, files, scenes, add-ons, rendering systems and workflows should remain accessible wherever technically possible.

AI functionality augments Blender rather than replacing its underlying capabilities.

2. Preserve GhostBlender Simple as an independent access route

GhostBlender Simple remains a supported external interface.

ChatGPT, Claude and other compatible external clients must continue to be able to connect directly to the running GHOSTpad instance without requiring GHOSTroom.

This route serves three purposes:

* full external agent control;
* development and diagnostics;
* recovery if the native GHOSTroom interface is unavailable.

GHOSTroom must therefore be an additional client of the GhostBlender architecture, not its replacement.

3. Create GHOSTroom as a native GHOSTpad experience

GHOSTroom will be a substantial native iPadOS interface using UIKit/SwiftUI-style controls rather than attempting to force conversational interaction into Blender’s traditional Python UI.

It should provide:

* a large, comfortable two-way conversation display;
* proper multiline text composition;
* native selection, copy, paste and scrolling;
* excellent software-keyboard behaviour;
* touch and Apple Pencil appropriate controls;
* attachments and context controls;
* agent selection;
* task/progress presentation;
* collapsible or dismissible presentation so Blender immediately regains the screen.

The existing Blender N-panel conversation prototype should be regarded as a transport proof-of-concept, not the final conversation UI.

4. Make GHOSTroom part of Blender rather than a separate app

GHOSTroom should visually overlay or coexist with the live Blender interface inside GHOSTpad.

Opening it should feel like opening another native GHOSTpad workspace, not leaving Blender for a chatbot.

The Blender viewport should remain immediately accessible beneath or beside it.

5. Maintain one authoritative Blender-control layer

There must be only one canonical mechanism through which agents manipulate Blender:

GhostBlender Tool Layer → Blender main thread

GHOSTroom, GhostBlender Simple, Codex, Claude, Gemini and future agents must not develop separate implementations of Blender manipulation.

All clients use the same capabilities, safety rules, job serialization and evidence mechanisms.

6. Introduce an Agent Router

The relay should cease being conceptually tied to Codex.

An Agent Router should allow GHOSTroom to direct a conversation or task to whichever configured agent is selected.

Providers and agents become adapters around a common interface.

Potential examples include Codex, Claude, Gemini, OpenAI-backed agents and future compatible systems.

GHOSTpad should know each agent’s:

* availability;
* authentication state;
* model/provider identity;
* relevant capabilities;
* quota or rate-limit state where obtainable;
* active task state.

The interface should tell the user that an agent is unavailable before possible, rather than collapsing everything into “AI error.”

7. Give agents independent lanes without creating information silos

Every agent can maintain its own working lane, thread and short-term context.

However, no information necessary to understand or continue the project may exist exclusively in an agent’s private lane.

Switching agents must not mean starting again.

An agent entering an existing project should be able to determine what other agents have done, why they did it, what failed, what remains unresolved and what the current Blender state represents.

8. Create a Shared Workspace Ledger

GHOSTpad should maintain an agent-neutral persistent record of meaningful project activity.

The ledger should be capable of recording:

* user instructions;
* agent responses;
* tasks;
* tool calls;
* Blender mutations;
* inspections;
* screenshots and rendered evidence;
* files opened or saved;
* checkpoints;
* failures;
* warnings;
* decisions;
* reviews;
* handoffs;
* unresolved questions;
* useful summaries.

Entries identify their origin, for example User, ChatGPT, Claude, Codex, Gemini, GhostBlender, rather than being owned by that origin.

The ledger becomes the recoverable memory of the working session.

9. Support project recovery

GHOSTpad should eventually be able to answer questions such as:

“Where were we before Blender crashed?”

“What did Claude change to this rig?”

“Why did we abandon the previous cloth setup?”

“Which version was working?”

“What was Astra planning to do next?”

Recovery should rely on the shared ledger and actual Blender evidence rather than whichever model happens to remember the conversation.

10. Enable genuine multi-agent collaboration

Multiple agents should be able to work on the same GHOSTpad project through defined collaboration roles rather than merely existing in a model selector.

Initial roles should support concepts such as:

Primary: responsible for advancing the task.
Reviewer: checks another agent’s proposal or result.
Specialist: investigates a particular technical or creative area.
Critic: deliberately searches for weaknesses or overlooked problems.
Verifier: tests whether the result actually meets the target.

Agents should be able to leave structured tasks, findings and review requests for one another through the shared workspace.

11. Prevent multi-agent scene collisions

Multiple agents may inspect concurrently where safe.

Actual scene-changing work should be serialized through an edit lease/lock or equivalent mechanism.

GHOSTpad should always know which agent currently has authority to mutate Blender.

This prevents two autonomous agents from independently changing the same scene at the same time.

12. Make agent collaboration visible to the user

GHOSTroom should eventually expose agent activity rather than hiding it.

For example:

Claude · reviewing cloth topology
ChatGPT · waiting for review
Gemini · analysing reference image
GhostBlender · rendering verification capture

The user should be able to understand who is doing what and intervene when desired.

13. Treat evidence as part of AI work

Agents should not merely issue Blender commands and declare success.

Where appropriate they should inspect:

* scene state;
* selected objects;
* topology;
* transforms;
* modifiers;
* animation;
* logs;
* screenshots;
* viewport captures;
* render results.

Visual work should normally include visual verification.

The shared ledger should retain important evidence so another agent can inspect the same basis for a decision.

14. Give the user explicit context controls

GHOSTroom should make it easy to attach or expose relevant context to an agent, including eventually:

Current Scene
Selected Object
Viewport
Screenshot
Render Result
Image/Reference
File

Agents should still be capable of using GhostBlender inspection tools themselves. Context buttons supplement that ability rather than replacing it.

15. Distinguish discussion, inspection and action

GHOSTroom should clearly represent whether an agent is:

talking about something;

examining Blender;

planning a change;

executing a change;

waiting;

reviewing the outcome;

or requesting user input.

This becomes increasingly important once several agents can participate.

16. Design for long-running work

Blender and AI tasks can take substantial time.

GHOSTroom should present useful progress states rather than generic loading indicators:

Inspecting scene
Analysing mesh
Applying modifier changes
Waiting for Blender
Capturing viewport
Reviewing result
Requesting second-agent review

The system should survive temporary app interruption and relay interruptions wherever possible.

17. Build robust failure and recovery semantics

Failures should be specific.

Examples:

Agent quota exhausted
Provider unavailable
Authentication expired
Relay unavailable
Blender suspended
Tool timeout
Scene changed during operation
Agent turn interrupted after possible modification

A failed agent should not imply a failed bridge.

Where safe, switching to another available agent should allow the work to continue.

18. Make teachability a foundational capability

GHOSTroom must be capable of evolving beyond “do this for me.”

Its interaction model should eventually support:

Do it for me
The agent performs the task.

Do it with me
The human and agent divide the work.

Teach me
The agent instructs the user and waits for them to perform steps.

Explain this
The agent inspects the actual current Blender state and explains it.

Review my work
The user performs the work and the agent assesses it.

This should influence today’s data structures even if the education product is built later.

19. Allow agents to observe learner actions

For future learning workflows, GhostBlender should be capable of detecting relevant changes such as:

* object selection;
* mode changes;
* modifier/settings changes;
* transformations;
* edits;
* animation changes;
* rendering;
* completion of a requested step.

A lesson can therefore progress according to what the learner actually does rather than simply displaying static instructions.

20. Support adaptive learning

Longer term, GHOSTroom could transform a Blender task into an interactive guided lesson.

The agent could demonstrate, explain, wait, inspect the result, diagnose a mistake and alter the next instruction accordingly.

This creates the foundation for a separately sellable learning product rather than conventional prerecorded tutorials.

21. Keep the portal content model richer than chat bubbles

Although conversation is the initial interface, GHOSTroom should be designed to render richer objects later:

* instructional cards;
* screenshots;
* before/after comparisons;
* agent review cards;
* tasks;
* warnings;
* progress;
* interactive choices;
* Blender object references;
* lesson steps;
* checkpoints;
* generated assets.

The internal protocol should therefore describe events and artifacts, not merely strings of chat text.

22. Establish a distinct GHOSTpad visual language

We can take strong inspiration from Higgsfield’s decision to give AI a generous, branded interaction surface, but GHOSTroom should not visually imitate Higgsfield.

It should become recognisably GHOSTpad.

Native Apple interaction quality should meet a distinctive creative identity rather than resembling either stock Blender or a generic web chat.

23. Keep native and Blender responsibilities clean

The native GHOSTpad shell should own things iPadOS is excellent at:

text composition, keyboard interaction, windows/overlays, gestures, files, media picking, rich message presentation and native navigation.

Blender should own Blender.

GhostBlender should mediate between them.

The relay/router should coordinate remote agents and durable collaborative state.

This separation will make the system substantially easier to maintain.

24. Do not make any individual AI vendor structurally essential

Codex is an agent implementation, not GHOSTpad’s brain.

Claude is an agent implementation.

Gemini is an agent implementation.

Any one of them should be replaceable without rewriting GHOSTroom, GhostBlender or the Shared Workspace Ledger.

The durable asset is the collaboration architecture.

25. Ultimate product target

The eventual experience should be:

Open GHOSTpad. Work directly in full Blender when you want. Open GHOSTroom when you want assistance. Talk naturally to whichever agent you choose. Let it inspect the same live project you’re looking at. Ask another agent for a second opinion. Watch them collaborate. Take control yourself at any point. Return tomorrow and recover exactly where everyone left off. Or switch into teaching mode and have the same system teach you how to perform the work yourself.

That, I think, is the target worth building toward.
