import os
import sys
import subprocess
import shutil
from pathlib import Path

def check_python_dependencies():
    """Check and install required Python packages."""
    required_packages = ['gmsh', 'numpy']
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            missing_packages.append(package)
    
    if missing_packages:
        print(f"Installing missing packages: {', '.join(missing_packages)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing_packages)

def setup_gmsh():
    """Set up GMSH for Windows."""
    try:
        import gmsh
        return True
    except ImportError:
        print("GMSH Python package not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "gmsh"])
        return True

def setup_windows_paths():
    """Set up Windows-specific paths and permissions."""
    # Get the extension directory
    extension_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Create necessary directories if they don't exist
    os.makedirs(os.path.join(extension_dir, "temp"), exist_ok=True)
    
    # Make sure generate_mesh.py is executable
    mesh_script = os.path.join(extension_dir, "generate_mesh.py")
    if os.path.exists(mesh_script):
        # Ensure the script has proper line endings for Windows
        with open(mesh_script, 'r') as f:
            content = f.read()
        with open(mesh_script, 'w', newline='\r\n') as f:
            f.write(content)
        
        # Make the script executable
        os.chmod(mesh_script, 0o755)

def verify_setup():
    """Verify that all components are properly set up."""
    try:
        # Check Python dependencies
        check_python_dependencies()
        
        # Set up GMSH
        if not setup_gmsh():
            raise Exception("Failed to set up GMSH")
        
        # Set up Windows paths
        setup_windows_paths()
        
        # Test mesh generation script
        extension_dir = os.path.dirname(os.path.abspath(__file__))
        mesh_script = os.path.join(extension_dir, "generate_mesh.py")
        
        if not os.path.exists(mesh_script):
            raise Exception(f"Mesh generation script not found at: {mesh_script}")
        
        print("Setup completed successfully!")
        return True
        
    except Exception as e:
        print(f"Setup failed: {str(e)}")
        return False

if __name__ == "__main__":
    verify_setup() 