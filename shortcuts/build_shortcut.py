"""Build an unsigned Apple Shortcuts template with no embedded credentials.

Sign on macOS before sharing. See README.md for the no-Mac setup recipe.
Only Python's standard library is required.
"""

from pathlib import Path
import plistlib
from uuid import NAMESPACE_URL, uuid5


def identifier(name):
    return str(uuid5(NAMESPACE_URL, "yt-dlp-web-shortcut:" + name)).upper()


def output(name, label):
    return {"Type": "ActionOutput", "OutputUUID": identifier(name), "OutputName": label}


def attachment(value):
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def text(value="", variable=None):
    data = {"string": value + ("\ufffc" if variable else "")}
    if variable:
        data["attachmentsByRange"] = {f"{{{len(value)}, 1}}": variable}
    return {"Value": data, "WFSerializationType": "WFTextTokenString"}


def dictionary(items):
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": text(key), "WFValue": value} for key, value in items.items()
    ]}, "WFSerializationType": "WFDictionaryFieldValue"}


def action(kind, name, **parameters):
    return {"WFWorkflowActionIdentifier": "is.workflow.actions." + kind,
            "WFWorkflowActionParameters": {"UUID": identifier(name), **parameters}}


def build():
    group = identifier("response-branch")
    actions = [
        action("gettext", "endpoint", WFTextActionText="https://your-server.example/api/v1/downloads"),
        action("gettext", "token", WFTextActionText=""),
        action("detect.link", "urls", WFInput=text(variable={"Type": "ExtensionInput"})),
        action("getitemfromlist", "first-url", WFInput=attachment(output("urls", "URLs")), WFItemSpecifier="First Item"),
        action("downloadurl", "request", WFURL=text(variable=output("endpoint", "Text")),
               WFHTTPMethod="POST", WFHTTPBodyType="JSON", ShowHeaders=True,
               WFHTTPHeaders=dictionary({"Authorization": text("Bearer ", output("token", "Text"))}),
               WFJSONValues=dictionary({"url": text(variable=output("first-url", "Item from List"))})),
        action("getvalueforkey", "job-id", WFInput=attachment(output("request", "Contents of URL")),
               WFGetDictionaryValueType="Value", WFDictionaryKey="job.id"),
        action("conditional", "if-job", GroupingIdentifier=group, WFControlFlowMode=0,
               WFCondition=100, WFInput=attachment(output("job-id", "Dictionary Value"))),
        action("notification", "success", WFNotificationActionTitle="yt-dlp-web",
               WFNotificationActionBody=text("Download accepted: ", output("job-id", "Dictionary Value"))),
        action("conditional", "else", GroupingIdentifier=group, WFControlFlowMode=1),
        action("showresult", "error", Text=text("Server response: ", output("request", "Contents of URL"))),
        action("conditional", "end", GroupingIdentifier=group, WFControlFlowMode=2),
    ]
    return {
        "WFWorkflowName": "Download with yt-dlp-web", "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "3036.0.4", "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900", "WFWorkflowHasOutputFallback": False,
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 61440},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFSafariWebPageContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowImportQuestions": [
            {"Category": "Parameter", "ActionIndex": 0, "ParameterKey": "WFTextActionText",
             "Text": "API URL",
             "DefaultValue": "https://your-server.example/api/v1/downloads"},
            {"Category": "Parameter", "ActionIndex": 1, "ParameterKey": "WFTextActionText",
             "Text": "Token (optional)", "DefaultValue": ""},
        ],
    }


if __name__ == "__main__":
    path = Path(__file__).with_name("Download with yt-dlp-web.unsigned.shortcut")
    path.write_bytes(plistlib.dumps(build(), fmt=plistlib.FMT_BINARY, sort_keys=False))
    print(path)
