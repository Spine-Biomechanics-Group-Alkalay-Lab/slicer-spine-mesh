# Spine Mesh Generator

A 3D Slicer module for generating high-quality tetrahedral meshes from spine segmentations for finite element analysis.

> **IMPORTANT NOTE**: This documentation is for developers and research use only. This is not an officially supported extension from the 3D Slicer team.

## Overview

This module automates the process of creating uniform meshes from CT scans and segmentations for finite element analysis of spine segments. It supports:

- Easy selection of segments to process
- Target edge length specification
- Material property mapping from CT Hounsfield Units
- Multiple output mesh formats (VTK, STL, Abaqus INP, GMSH, Summit)
- Automatic mesh quality analysis and visualization

## Installation for Developers

### Prerequisites

- 3D Slicer (version 5.0.0 or newer recommended)
- Python packages (will be automatically installed):
  - meshio
  - pyacvd
  - SimpleITK
  - tqdm

### Manual Installation (Developer Use Only)

1. Clone the repository: `git clone https://github.com/yourusername/SpineMeshGenerator.git`
2. Open 3D Slicer
3. Go to `Edit → Application Settings → Modules`
4. Add the path to the cloned repository to "Additional module paths"
5. Restart 3D Slicer

## Accessing the Module

After installation:

1. Open 3D Slicer
2. Go to the modules dropdown menu in the top toolbar
3. Navigate to `FE-Spine → Spine Mesh Generator`
   - If you don't see this category, click on "All Modules" at the top of the dropdown
   - You can also use the search bar at the top of the modules panel and type "Spine Mesh"
4. The Spine Mesh Generator interface will appear in the module panel on the left side of the screen

## Usage

### Quick Start

1. Load a CT volume into 3D Slicer
2. Create segmentations of spine segments you want to mesh
3. Open the `Spine Mesh Generator` module from the `FE-Spine` category
4. Select your CT volume in the "Input CT Volume" dropdown
5. Choose segments to process from the segment list
6. Specify desired mesh parameters (target edge length, output format)
7. Select an output directory
8. Click "Apply" to generate meshes

### Detailed Steps

#### 1. Preparing Inputs

- Load a CT scan using the `Data` module
- Create segmentations of each vertebra or spine structure using the `Segment Editor` module
- Make sure segmentations are properly labeled (e.g., "L1", "L2", "L3", etc.)

#### 2. Configuring Mesh Generation Parameters

- **Target Edge Length**: Set the desired edge length in mm for the tetrahedral elements (default: 1.37mm)
- **Output Format**: Choose from:
  - All Formats (generates all available formats)
  - VTK (for visualization)
  - STL (surface mesh only)
  - Abaqus INP (for Abaqus FEA software)
  - GMSH (for GMSH software)
  - Summit (custom format for Summit FEA software)

#### 3. Material Mapping (Optional)

Enable material mapping to calculate material properties based on CT Hounsfield Units:

- Check "Enable Material Mapping from CT"
- Specify the calibration parameters:
  - Slope (mg/cm³ per HU): Default 0.7
  - Intercept (mg/cm³): Default 5.1

This creates element property files that map bone mineral density and bone volume fraction to each element.

#### 4. Generating Meshes

- Select which segments to process (use "Select All" or "Deselect All" for convenience)
- Choose an output directory where mesh files will be saved
- Click "Apply" to start the mesh generation process
- The generated meshes will automatically appear in the 3D view for inspection

#### 5. Reviewing Results

After processing, a results dialog shows:
- Total number of elements
- Average edge length
- Per-segment statistics

The generated mesh models are also named with element count and edge length information for easy reference.

## Output Files

For each processed segment, the following files are generated in the output directory:

```
OutputDirectory/
├── SegmentName1/
│   ├── SegmentName1_surface_mesh.stl       # Surface mesh (triangles)
│   ├── SegmentName1_volume_mesh.vtk        # Volume mesh (tetrahedra)
│   ├── SegmentName1_volume_mesh.inp        # Abaqus format mesh
│   ├── SegmentName1_surface_mesh.msh       # GMSH format mesh
│   ├── SegmentName1_element_properties.csv # Material properties per element
│   ├── SegmentName1_statistics.csv         # Mesh quality metrics
│   └── SegmentName1_mesh.summit            # Summit format mesh
├── SegmentName2/
│   └── ...
```

### Mesh Statistics

The statistics CSV file contains detailed information about the mesh:
- Surface area and volume
- Number of triangles and tetrahedra
- Average edge length
- Element quality metrics
- Material property distribution (if enabled)

## Troubleshooting

### Common Issues

1. **Missing dependencies**: If you see errors about missing Python modules, try installing them manually using Slicer's Python:
   ```
   slicer.util.pip_install("meshio pyacvd SimpleITK tqdm")
   ```

2. **GMSH errors**: If mesh generation fails with GMSH errors:
   - Ensure your segmentations are watertight (no holes)
   - Try simplifying complex segmentations
   - Check if GMSH is properly installed

3. **Memory issues**: For large or complex segmentations:
   - Try increasing the target edge length to reduce mesh density
   - Process segments one at a time instead of all at once
   - Close other applications to free up memory

### Getting Help

If you encounter issues not covered here:
- Check the extension's GitHub repository for known issues
- Submit a detailed bug report with:
  - 3D Slicer version
  - Extension version
  - Error messages from the Python console
  - Sample data if possible

## Contributing

Contributions to improve the Spine Mesh Generator are welcome:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Based on the mesh automation pipeline developed by the FE-Spine group
- Uses open-source tools including GMSH, meshio, and pyacvd
- Integrates with 3D Slicer's SegmentStatistics and SurfaceToolbox modules
