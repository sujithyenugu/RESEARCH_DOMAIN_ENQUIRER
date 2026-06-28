# cdk/stacks/__init__.py
# Exposes all CDK stacks as a package.
# Import individual stacks as they are implemented each day.

from stacks.storage_stack import StorageStack

__all__ = [
    "StorageStack",
]
