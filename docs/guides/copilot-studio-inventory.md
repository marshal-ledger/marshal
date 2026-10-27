---
title: "Copilot Studio agent inventory and change history"
---

# Copilot Studio agent inventory and change history

Copilot Studio agents are usually built in a web designer, not in a repository, so there is no pull request to review and no file to diff. Marshal reads the solutions you export from Power Platform and keeps a versioned record of each agent: its model, instructions, tools, knowledge sources, and above all whose credentials each tool uses.

## 1. Export the solutions that hold your agents

```console
$ pac auth create --environment https://contoso.crm.dynamics.com
$ pac solution export --name ContosoHelpdesk --path ContosoHelpdesk.zip
```

Power Platform can't export a managed solution, so export the unmanaged solutions where makers build, usually in a development environment. A folder cloned with the Copilot Studio extension for Visual Studio Code works too.

## 2. Record them

```console
$ pip install marshal-ledger
$ marshal collect ContosoHelpdesk.zip --event shipped --environment prod
```

Each agent in the solution, and each of its sub-agents, becomes its own record. The format is detected; see [Copilot Studio](../formats/copilot-studio.md) for exactly what is read from each file.

## 3. Ask the questions an admin center can't

```console
$ marshal ls --platform copilot-studio
AGENT                          PLATFORM        LATEST  SHIPPED  FRESHNESS        COMPLETE
eval_testAgent                 copilot-studio  v1      prod v1  prod: manual 0d  38%
mpa_LeaveManagement            copilot-studio  v1      prod v1  prod: manual 0d  53%
test_allthethings              copilot-studio  v1      prod v1  prod: manual 0d  36%
test_allthethings/HelperAgent  copilot-studio  v1      prod v1  prod: manual 0d  30%
```

Which agents use the maker's credentials for some tools and each user's own for others?

```console
$ marshal ls --identity mixed
```

Which agents read a SharePoint site, or call a Dataverse operation?

```console
$ marshal ls --knowledge "*sharepoint*"
$ marshal deps "tool/powerplatform.shared_commondataserviceforapps.*"
```

A `Maker` connection runs every user's request with the credentials the agent's author configured. An `Invoker` connection uses each user's own. Marshal records the mode of every action and topic call, and summarizes them as the agent's identity, so an agent that quietly acts as its author for everyone stands out.

## 4. Keep it current

Run the export on a schedule and push it as an `observed` event. Marshal then reports any edit made in the web designer as drift from what was shipped. The [scheduled Copilot Studio workflow](https://github.com/marshal-ledger/marshal/blob/main/examples/scheduled/copilot-studio.yml) signs in to Power Platform with a federated credential, so no secret is stored, and [Detect drift](drift-detection.md) shows what a report looks like.

To share the inventory, push to a shared Marshal server instead of a local file, and browse it in the [web view](../ingest-server.md#web-view).
