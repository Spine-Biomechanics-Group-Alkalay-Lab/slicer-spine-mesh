#!/usr/bin/env python3
import sys
import gmsh

def generate_mesh():
    # Check command line arguments
    if len(sys.argv) < 3:
        print("Usage: python generate_mesh.py input.stl output.msh [element_size]")
        return
    
    input_stl = sys.argv[1]
    output_msh = sys.argv[2]
    size_param = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    
    # Initialize GMSH
    gmsh.initialize()
    
    # Create a new model
    gmsh.model.add("VolumeFromSTL")
    
    # Import STL file
    print(f"Merging STL file: {input_stl}")
    gmsh.merge(input_stl)
    
    # Get entities from the imported STL
    entities = gmsh.model.getEntities(dim=2)
    if not entities:
        print("No surfaces found.")
        gmsh.finalize()
        sys.exit(1)
    
    # Create volume from surface loop
    surface_tags = [entity[1] for entity in entities]
    loop = gmsh.model.geo.addSurfaceLoop(surface_tags)
    volume = gmsh.model.geo.addVolume([loop])
    
    # Synchronize the model
    gmsh.model.geo.synchronize()
    
    # Set mesh size parameters
    gmsh.option.setNumber('Mesh.MeshSizeMin', size_param)
    gmsh.option.setNumber('Mesh.MeshSizeMax', size_param)
    
    # Generate 3D mesh
    gmsh.model.mesh.generate(3)
    
    # Write the mesh to file
    gmsh.write(output_msh)
    
    # Finalize GMSH
    gmsh.finalize()
    
    print(f"Mesh generation complete. Saved to: {output_msh}")

if __name__ == "__main__":
    generate_mesh()