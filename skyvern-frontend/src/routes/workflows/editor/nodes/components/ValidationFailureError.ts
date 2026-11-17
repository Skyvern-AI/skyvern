// Thrown to abort a block run after the user has already been shown why it
// can't start (block errors, missing inputs). An expected outcome, not a failure.
export class ValidationFailureError extends Error {
  readonly isValidationFailure = true;
  constructor() {
    super("workflow validation failed");
    this.name = "ValidationFailureError";
  }
}
