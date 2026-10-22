"""Domain handlers: the code behind the tools declared in `alsense_chat/agent/<domain>/tools.yaml`.

Each domain module exposes `HANDLERS`, mapping a tool name to its argument schema, handler and audit helpers.
`alsense_chat.tools` combines them and checks at startup that every declared tool has a matching handler.
"""
