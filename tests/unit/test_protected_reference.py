import inspect

from skyvern.forge.sdk.protected_reference import ProtectedReferenceResolver


def test_resolver_contract_is_consumer_bound() -> None:
    signature = inspect.signature(ProtectedReferenceResolver.resolve)

    assert inspect.iscoroutinefunction(ProtectedReferenceResolver.resolve)
    assert tuple(signature.parameters) == ("self", "ref", "run_id", "consumer_id")
    assert signature.parameters["ref"].annotation == "ProtectedReference"
    assert signature.parameters["run_id"].annotation == "str"
    assert signature.parameters["consumer_id"].annotation == "str"
    assert signature.return_annotation == "str"
