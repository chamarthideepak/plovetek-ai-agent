# Plovetek Phase 1 starter

Run `python app.py` from this folder in the VS Code terminal. Requires Python 3.10+ and uses only the standard library.

Try these commands in order:

```text
seed
chat Which replies are pending?
chat Summarize enquiry-1
edit enquiry-1 Thank you for your enquiry. I will follow up with pricing.
approve enquiry-1
chat Show escalations
chat Summarize refund-1
chat Show tasks
```

Use `reject <email ID>` to reject a pending draft. To simulate a new email arriving, use:

```text
receive {"id":"test-1","sender":"test@example.com","subject":"Can you help?","body":"I have a question about your service."}
```

The same message ID is processed once. Demo state persists in demo.sqlite3. Close the app and remove that file if you want to reset the demo.

This starter uses sample email, keyword routing, a fixed draft template, and a command-based chatbot. The terminal operator acts as the owner in the simulation. Live Phase 1 still requires Google OAuth for one Gmail inbox, an automatic arrival trigger with retry handling, authenticated owner review, Gmail sending after approval, and a language model for summaries, drafting and chat. The Gmail owner will approve replies and handle escalations. The demo performs no external actions.
