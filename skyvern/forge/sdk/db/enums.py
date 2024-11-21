from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"


class TaskPromptTemplate(StrEnum):
    ExtractAction = "extract-action"
    DecisiveCriterionValidate = "decisive-criterion-validate"
    SingleClickAction = "single-click-action"
    SingleInputAction = "single-input-action"
    SingleUploadAction = "single-upload-action"
    SingleSelectAction = "single-select-action"


class ActionType(StrEnum):
    Click = "CLICK"
    InputText = "INPUT_TEXT"
    UploadFile = "UPLOAD_FILE"
    SelectOption = "SELECT_OPTION"
