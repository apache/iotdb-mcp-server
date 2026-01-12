#!/usr/bin/env python3
"""
Security Test Suite for Apache IoTDB MCP Server Path Traversal Fix
CVE-2026-XXXXX: Path Traversal leading to RCE
Author: Mohammed Tanveer (threatpointer)
Tests the sanitize_filename function to ensure the vulnerability is patched.
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

# Add the src directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Import the sanitize_filename function
from iotdb_mcp_server.server import sanitize_filename


class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def add_pass(self, test_name):
        self.passed += 1
        print(f"[PASS] {test_name}")

    def add_fail(self, test_name, reason):
        self.failed += 1
        self.errors.append((test_name, reason))
        print(f"[FAIL] {test_name}")
        print(f"  Reason: {reason}")

    def summary(self):
        total = self.passed + self.failed
        print("\n" + "=" * 70)
        print(f"TEST SUMMARY: {self.passed}/{total} tests passed")
        if self.failed > 0:
            print(f"\nFailed tests ({self.failed}):")
            for test_name, reason in self.errors:
                print(f"  - {test_name}: {reason}")
        print("=" * 70)
        return self.failed == 0


def test_path_traversal_attacks(temp_dir, results):
    """Test that path traversal attacks are blocked"""

    # Test 1: Simple path traversal
    try:
        sanitize_filename("../attack.csv", temp_dir)
        results.add_fail("Simple path traversal", "Should have raised ValueError")
    except ValueError as e:
        if "within export directory" in str(e) or "Invalid filename" in str(e):
            results.add_pass("Simple path traversal blocked")
        else:
            results.add_fail("Simple path traversal", f"Wrong error: {e}")
    except Exception as e:
        results.add_fail("Simple path traversal", f"Unexpected error: {e}")

    # Test 2: Multi-level path traversal
    try:
        sanitize_filename("../../../../etc/passwd.csv", temp_dir)
        results.add_fail("Multi-level path traversal", "Should have raised ValueError")
    except ValueError:
        results.add_pass("Multi-level path traversal blocked")
    except Exception as e:
        results.add_fail("Multi-level path traversal", f"Unexpected error: {e}")

    # Test 3: Path traversal with valid filename
    try:
        sanitize_filename("../../tmp/attack.csv", temp_dir)
        results.add_fail(
            "Path traversal with valid filename", "Should have raised ValueError"
        )
    except ValueError:
        results.add_pass("Path traversal with valid filename blocked")
    except Exception as e:
        results.add_fail("Path traversal with valid filename", f"Unexpected error: {e}")

    # Test 4: Absolute path attack
    try:
        if sys.platform == "win32":
            sanitize_filename("C:\\Windows\\System32\\attack.csv", temp_dir)
        else:
            sanitize_filename("/etc/passwd.csv", temp_dir)
        results.add_fail("Absolute path attack", "Should have raised ValueError")
    except ValueError:
        results.add_pass("Absolute path attack blocked")
    except Exception as e:
        results.add_fail("Absolute path attack", f"Unexpected error: {e}")


def test_valid_filenames(temp_dir, results):
    """Test that valid filenames are accepted"""

    valid_filenames = [
        "export.csv",
        "data_2024.xlsx",
        "my-file.csv",
        "test_file_123.csv",
        "UPPERCASE.CSV",
        "mixed_Case-File.123.csv",
    ]

    for filename in valid_filenames:
        try:
            result = sanitize_filename(filename, temp_dir)
            # Check that the result is within the temp directory
            if os.path.realpath(result).startswith(os.path.realpath(temp_dir)):
                results.add_pass(f"Valid filename accepted: {filename}")
            else:
                results.add_fail(
                    f"Valid filename: {filename}", "Path escaped export directory"
                )
        except Exception as e:
            results.add_fail(
                f"Valid filename: {filename}", f"Unexpected rejection: {e}"
            )


def test_invalid_characters(temp_dir, results):
    """Test that filenames with invalid characters are rejected"""

    invalid_filenames = [
        "file name.csv",  # space
        "file;name.csv",  # semicolon
        "file:name.csv",  # colon
        "file*name.csv",  # asterisk
        "file?name.csv",  # question mark
        "file|name.csv",  # pipe
        "file<name.csv",  # less than
        "file>name.csv",  # greater than
        'file"name.csv',  # quote
        "file\\name.csv",  # backslash
        "file/name.csv",  # forward slash
    ]

    for filename in invalid_filenames:
        try:
            sanitize_filename(filename, temp_dir)
            results.add_fail(
                f"Invalid character test: {filename}", "Should have been rejected"
            )
        except ValueError as e:
            if "Invalid filename" in str(e):
                results.add_pass(f"Invalid character rejected: {filename}")
            else:
                results.add_fail(f"Invalid character: {filename}", f"Wrong error: {e}")
        except Exception as e:
            results.add_fail(f"Invalid character: {filename}", f"Unexpected error: {e}")


def test_edge_cases(temp_dir, results):
    """Test edge cases"""

    # Test 1: Empty filename
    try:
        sanitize_filename("", temp_dir)
        results.add_fail("Empty filename", "Should have raised ValueError")
    except ValueError:
        results.add_pass("Empty filename rejected")
    except Exception as e:
        results.add_fail("Empty filename", f"Unexpected error: {e}")

    # Test 2: Just dots
    try:
        sanitize_filename("..", temp_dir)
        results.add_fail("Just dots", "Should have raised ValueError")
    except ValueError:
        results.add_pass("Just dots rejected")
    except Exception as e:
        results.add_fail("Just dots", f"Unexpected error: {e}")

    # Test 3: Single dot
    try:
        sanitize_filename(".", temp_dir)
        results.add_fail("Single dot", "Should have raised ValueError")
    except ValueError:
        results.add_pass("Single dot rejected")
    except Exception as e:
        results.add_fail("Single dot", f"Unexpected error: {e}")

    # Test 4: Hidden file (starts with dot) - should be valid
    try:
        result = sanitize_filename(".hidden.csv", temp_dir)
        if os.path.realpath(result).startswith(os.path.realpath(temp_dir)):
            results.add_pass("Hidden file accepted")
        else:
            results.add_fail("Hidden file", "Path escaped export directory")
    except Exception as e:
        results.add_fail("Hidden file", f"Unexpected error: {e}")


def test_boundary_validation(temp_dir, results):
    """Test that files cannot escape the export directory"""

    # Create a subdirectory in temp
    subdir = os.path.join(temp_dir, "subdir")
    os.makedirs(subdir, exist_ok=True)

    # Test 1: File in parent directory
    try:
        # Try to write to parent of temp_dir
        parent_dir = os.path.dirname(temp_dir)
        filename = os.path.join("..", os.path.basename(parent_dir), "attack.csv")
        sanitize_filename(filename, temp_dir)
        results.add_fail("Escape to parent directory", "Should have been blocked")
    except ValueError:
        results.add_pass("Escape to parent directory blocked")
    except Exception as e:
        results.add_fail("Escape to parent directory", f"Unexpected error: {e}")

    # Test 2: Verify normalized path stays in boundary
    try:
        result = sanitize_filename("test.csv", temp_dir)
        result_real = os.path.realpath(result)
        temp_real = os.path.realpath(temp_dir)

        if result_real.startswith(temp_real):
            results.add_pass("Normalized path within boundary")
        else:
            results.add_fail("Normalized path boundary", f"Path escaped: {result_real}")
    except Exception as e:
        results.add_fail("Normalized path boundary", f"Error: {e}")


def test_exploit_scenarios(temp_dir, results):
    """Test the actual exploit scenarios from the security report"""

    exploit_filenames = [
        "../../../tmp/test_traversal.csv",
        "../../../../Windows/Startup/backdoor.csv",
        "../../../etc/cron.d/backdoor.csv",
        "../../../../../../home/user/.ssh/authorized_keys.csv",
        "../../../../var/www/html/shell.csv",
    ]

    for filename in exploit_filenames:
        try:
            sanitize_filename(filename, temp_dir)
            results.add_fail(f"Exploit scenario: {filename}", "Exploit not blocked!")
        except ValueError:
            results.add_pass(f"Exploit scenario blocked: {filename}")
        except Exception as e:
            results.add_fail(f"Exploit scenario: {filename}", f"Unexpected error: {e}")


def main():
    print("=" * 70)
    print("Apache IoTDB MCP Server - Security Patch Validation Tests")
    print("CVE-2026-XXXXX: Path Traversal leading to RCE")
    print("Author: Mohammed Tanveer (threatpointer)")
    print("=" * 70 + "\n")

    results = TestResults()

    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Test export directory: {temp_dir}\n")

        print("Running Path Traversal Attack Tests...")
        test_path_traversal_attacks(temp_dir, results)
        print()

        print("Running Valid Filename Tests...")
        test_valid_filenames(temp_dir, results)
        print()

        print("Running Invalid Character Tests...")
        test_invalid_characters(temp_dir, results)
        print()

        print("Running Edge Case Tests...")
        test_edge_cases(temp_dir, results)
        print()

        print("Running Boundary Validation Tests...")
        test_boundary_validation(temp_dir, results)
        print()

        print("Running Exploit Scenario Tests...")
        test_exploit_scenarios(temp_dir, results)
        print()

    # Print summary
    success = results.summary()

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
