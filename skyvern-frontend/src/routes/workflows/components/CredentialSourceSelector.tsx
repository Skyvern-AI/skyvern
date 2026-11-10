import {
  usePageSlots,
  type BrokerCredentialSelectorProps,
} from "@/store/PageSlots";
import { GoogleOAuthCredentialSelector } from "./GoogleOAuthCredentialSelector";
import {
  GOOGLE_GMAIL_REQUIRED_SCOPES,
  GOOGLE_GMAIL_SEND_REQUIRED_SCOPES,
} from "@/util/googleScopes";

export function CredentialSourceSelector(props: BrokerCredentialSelectorProps) {
  const { brokerCredentialSelector: BrokerSelector } = usePageSlots();
  if (BrokerSelector) return <BrokerSelector {...props} />;
  if (props.provider === "gmail") {
    return (
      <div className="ph-no-capture space-y-2" data-private>
        {props.credentialSource === "nango" ? (
          <p role="status" className="text-xs text-muted-foreground">
            Saved Gmail account is unavailable. Pick another account.
          </p>
        ) : null}
        <GoogleOAuthCredentialSelector
          nodeId={props.nodeId}
          value={props.credentialSource === "native" ? props.credentialId : ""}
          onChange={(id) => props.onChange("native", id)}
          requiredScopes={
            props.purpose === "send" || props.purpose === "reply"
              ? GOOGLE_GMAIL_SEND_REQUIRED_SCOPES
              : GOOGLE_GMAIL_REQUIRED_SCOPES
          }
          gmailSendOnly={props.purpose === "send" || props.purpose === "reply"}
          disabled={props.disabled}
          ariaLabel="Gmail account"
          showEmail
          additionalAccounts={{
            options: [],
            selectedId:
              props.credentialSource === "nango"
                ? props.connectionId
                : undefined,
            onSelect: () => {},
          }}
        />
      </div>
    );
  }
  return (
    <p role="status" className="text-xs text-muted-foreground">
      Power BI connections are unavailable in this environment. No native
      fallback will be used.
    </p>
  );
}
