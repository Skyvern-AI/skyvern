#!/usr/bin/env python3
"""
Script to fix alembic revision issues when switching between branches.

This commonly happens when working on multiple branches with different migrations.
The database stores a revision ID that doesn't exist in the current branch's migrations.
"""

import os
import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def main():
    """Fix alembic revision issues by stamping the head revision."""
    print("🔧 Fixing Alembic Revision Issues")
    print("=" * 50)
    
    try:
        # Import alembic after adding project to path
        from alembic import command
        from alembic.config import Config
        from skyvern.forge.sdk.settings_manager import SettingsManager
        
        # Get the alembic config
        alembic_cfg = Config("alembic.ini")
        
        # Set the database URL from settings
        database_url = SettingsManager.get_settings().DATABASE_STRING
        alembic_cfg.set_main_option("sqlalchemy.url", database_url)
        
        print(f"📍 Using database: {database_url}")
        print("🔄 Attempting to stamp head revision...")
        
        # Stamp the head revision
        command.stamp(alembic_cfg, "head")
        
        print("✅ Successfully stamped head revision!")
        print("🎉 Alembic revision issue fixed!")
        print("\nYou can now run 'alembic check' to verify the fix.")
        
    except ImportError as e:
        print(f"❌ Error importing required modules: {e}")
        print("Make sure you're running this from the project root directory.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error fixing alembic revision: {e}")
        if "Can't locate revision identified by" in str(e):
            print("\n💡 The database contains a revision that doesn't exist in this branch.")
            print("This is normal when switching between branches with different migrations.")
            print("\n🔧 Manual fix:")
            print("1. Connect to your database directly")
            print("2. Run: UPDATE alembic_version SET version_num = 'afeed80576cb';")
            print("3. Or delete the alembic_version table and recreate your database")
        sys.exit(1)

if __name__ == "__main__":
    main() 