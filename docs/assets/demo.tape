# The terminal recording in the README. Regenerate with marshal on PATH:
#   vhs docs/assets/demo.tape
# https://github.com/charmbracelet/vhs

Output docs/assets/demo.gif

Set Shell bash
Set FontSize 14
Set Width 1300
Set Height 440
Set Padding 20
Set Theme "GitHub Dark"
Set TypingSpeed 35ms

Hide
Type `export PS1='$ ' MARSHAL_DB=demo.sqlite && cd "$(mktemp -d)" && clear`
Enter
Show

Type "# Build a ledger from 28 public agent definitions"
Enter
Type "marshal demo"
Sleep 300ms
Enter
Wait+Screen@60s /Browse it/
Sleep 2s

Type "clear"
Enter
Type "# Someone switched a Copilot Studio tool to the maker's credentials in the web designer"
Enter
Type "marshal status eval_testAgent"
Sleep 300ms
Enter
Sleep 2s
Type "marshal diff eval_testAgent"
Sleep 300ms
Enter
Sleep 4s
Type "clear"
Enter

Type "# A pull request gives a Databricks agent a refund function"
Enter
Type "marshal diff agent_openai_agents_sdk/agent_openai_agents_sdk"
Sleep 300ms
Enter
Sleep 5s
