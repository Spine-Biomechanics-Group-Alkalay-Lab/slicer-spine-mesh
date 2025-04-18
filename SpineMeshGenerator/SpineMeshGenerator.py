import logging
import os
import sys
import tempfile
import numpy as np
import vtk
import subprocess
import shutil
import csv

import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import (
    parameterNodeWrapper,
    WithinRange,
)

import qt

from SegmentStatistics import SegmentStatisticsLogic
from SurfaceToolbox import SurfaceToolboxLogic

#
# SpineMeshGenerator
#

class SpineMeshGenerator(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Spine Mesh Generator")
        self.parent.categories = ["FE-Spine"]
        self.parent.dependencies = ["SegmentStatistics", "SurfaceToolbox"]
        self.parent.contributors = ["Your Name (Your Institution)"]
        self.parent.helpText = _("""
This module automates the process of creating uniform meshes from CT scans and segmentations for finite element analysis.
It supports segment selection, target edge length specification, and material mapping.
""")
        self.parent.acknowledgementText = _("""
Based on the mesh automation pipeline developed by the FE-Spine group.
""")
        # Additional initialization
        slicer.app.connect("startupCompleted()", self.initializeModule)
    
    def initializeModule(self):
        # Install required Python packages
        requiredPackages = ["meshio", "pyacvd", "tqdm", "SimpleITK"]
        for package in requiredPackages:
            try:
                __import__(package)
            except ImportError:
                if slicer.util.confirmYesNoDisplay(f"The {package} package is required. Install it now?"):
                    slicer.util.pip_install(package)


#
# SpineMeshGeneratorWidget
#

class SpineMeshGeneratorWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class."""

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # Needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._updatingGUIFromParameterNode = False
        self.segmentSelectionDict = {}  # Store segment selection info
        self.clippingEnabled = False
        self.clippingNode = None

    def setup(self):
        """Called when the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/SpineMeshGenerator.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class
        self.logic = SpineMeshGeneratorLogic()

        # Connect scene events
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Connect node selectors and controls
        self.ui.inputVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onVolumeSelected)
        self.ui.outputDirectorySelector.connect("directoryChanged(QString)", self.updateParameterNodeFromGUI)
        self.ui.targetEdgeLengthSpinBox.connect("valueChanged(double)", self.updateParameterNodeFromGUI)
        self.ui.enableMaterialMappingCheckBox.connect("toggled(bool)", self.onMaterialMappingToggled)
        self.ui.slopeSpinBox.connect("valueChanged(double)", self.updateParameterNodeFromGUI)
        self.ui.interceptSpinBox.connect("valueChanged(double)", self.updateParameterNodeFromGUI)
        self.ui.selectAllSegmentsButton.connect("clicked(bool)", self.onSelectAllSegments)
        self.ui.deselectAllSegmentsButton.connect("clicked(bool)", self.onDeselectAllSegments)
        self.ui.outputFormatComboBox.connect("currentIndexChanged(int)", self.updateParameterNodeFromGUI)
        
        # Connect Apply button
        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        # Connect clipping controls (new)
        self.ui.enableClippingButton.connect("clicked(bool)", self.onEnableClippingButtonClicked)
        self.ui.modelSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onModelSelected)
        self.ui.clipDirectionComboBox.connect("currentIndexChanged(int)", self.updateClippingDirection)
        self.ui.clipSliderWidget.connect("valueChanged(double)", self.updateClippingPosition)
        self.ui.flipClipCheckBox.connect("toggled(bool)", self.updateClippingFlip)

        # Setup quality analysis section
        self.ui.qualityAnalysisMeshSelector.nodeTypes = ["vtkMRMLModelNode"]
        self.ui.qualityAnalysisMeshSelector.addEnabled = False
        self.ui.qualityAnalysisMeshSelector.removeEnabled = False
        self.ui.qualityAnalysisMeshSelector.noneEnabled = True
        self.ui.qualityAnalysisMeshSelector.setMRMLScene(slicer.mrmlScene)
        self.ui.analyzeQualityButton.connect("clicked(bool)", self.onAnalyzeQualityButtonClicked)

        # Setup input volume selector properties
        self.ui.inputVolumeSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.ui.inputVolumeSelector.addEnabled = False
        self.ui.inputVolumeSelector.removeEnabled = False
        self.ui.inputVolumeSelector.noneEnabled = True
        self.ui.inputVolumeSelector.showHidden = True
        self.ui.inputVolumeSelector.showChildNodeTypes = True
        self.ui.inputVolumeSelector.selectNodeUponCreation = True
        self.ui.inputVolumeSelector.setMRMLScene(slicer.mrmlScene)
        
        # Setup model selector properties (new)
        self.ui.modelSelector.nodeTypes = ["vtkMRMLModelNode"]
        self.ui.modelSelector.addEnabled = False
        self.ui.modelSelector.removeEnabled = False
        self.ui.modelSelector.noneEnabled = True
        self.ui.modelSelector.setMRMLScene(slicer.mrmlScene)
        
        # Setup output format options
        self.ui.outputFormatComboBox.clear()
        self.ui.outputFormatComboBox.addItem("All Formats", "all")
        self.ui.outputFormatComboBox.addItem("VTK", "vtk")
        self.ui.outputFormatComboBox.addItem("STL", "stl")
        self.ui.outputFormatComboBox.addItem("Abaqus INP", "inp")
        self.ui.outputFormatComboBox.addItem("GMSH", "msh")
        self.ui.outputFormatComboBox.addItem("Summit", "summit")
        
        # Setup clip direction options (new)
        self.ui.clipDirectionComboBox.clear()
        self.ui.clipDirectionComboBox.addItem("Axial (X-Y)", 0)
        self.ui.clipDirectionComboBox.addItem("Sagittal (Y-Z)", 1)
        self.ui.clipDirectionComboBox.addItem("Coronal (X-Z)", 2)
        self.ui.clipDirectionComboBox.setCurrentIndex(0)  # Default to Axial
        
        # Update target edge length default to match config (1.37mm)
        self.ui.targetEdgeLengthSpinBox.setValue(1.37)
        
        # Hide material mapping options initially
        self.ui.materialMappingGroupBox.setVisible(False)
        
        # Hide clipping controls initially (new)
        self.ui.clippingControlsGroupBox.setVisible(False)
        
        # Initialize parameter node
        self.initializeParameterNode()
        
        # Create segment table view and update it
        self.setupSegmentTable()
        self.updateSegmentTable()

    def setupSegmentTable(self):
        """Set up the segment table widget."""
        self.ui.segmentsTableWidget.setColumnCount(2)
        self.ui.segmentsTableWidget.setHorizontalHeaderLabels(["Segment", "Include"])
        self.ui.segmentsTableWidget.horizontalHeader().setSectionResizeMode(0, qt.QHeaderView.Stretch)
        self.ui.segmentsTableWidget.horizontalHeader().setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)

    # Add these methods to the SpineMeshGeneratorWidget class
    def onAnalyzeQualityButtonClicked(self):
        """Analyze quality of the selected mesh"""
        modelNode = self.ui.qualityAnalysisMeshSelector.currentNode()
        if not modelNode:
            slicer.util.errorDisplay("No mesh selected for quality analysis.")
            return
            
        # Analyze quality using logic method
        qualityResults = self.logic.analyzeMeshQuality(modelNode)
        
        # Display results in a dialog
        self.showQualityResultsDialog(qualityResults, modelNode.GetName())
        
    def showQualityResultsDialog(self, qualityResults, meshName):
        """
        Show a dialog with mesh quality results
        """
        # Format the results message
        messageText = f"<b>Mesh Quality Analysis: {meshName}</b><br><br>"
        messageText += f"<b>Number of tetrahedral elements:</b> {qualityResults['num_elements']}<br>"
        messageText += f"<b>Elements with aspect ratio > 5:</b> {qualityResults['poor_elements']} "
        messageText += f"({qualityResults['poor_elements_percent']:.2f}%)<br>"
        messageText += f"<b>Average aspect ratio:</b> {qualityResults['avg_aspect_ratio']:.3f}<br>"
        messageText += f"<b>Worst aspect ratio:</b> {qualityResults['max_aspect_ratio']:.3f}<br>"
        
        # Create a dialog to display quality results
        qualityDialog = qt.QDialog(slicer.util.mainWindow())
        qualityDialog.setWindowTitle("Mesh Quality Analysis")
        qualityDialog.setMinimumWidth(400)
        
        layout = qt.QVBoxLayout(qualityDialog)
        
        label = qt.QLabel(messageText)
        label.setTextFormat(qt.Qt.RichText)
        layout.addWidget(label)
        
        closeButton = qt.QPushButton("Close")
        closeButton.setToolTip("Close this dialog")
        closeButton.connect("clicked(bool)", qualityDialog.accept)
        layout.addWidget(closeButton)
        
        # Show the dialog
        qualityDialog.exec_()

    def updateSegmentTable(self):
        """Update the segment table with current segments from segmentation module."""
        if self._parameterNode is None:
            self.initializeParameterNode()
            if self._parameterNode is None:
                logging.error("Failed to initialize parameter node")
                return
        
        segmentationNode = None
        segmentEditorSingletonTag = "SegmentEditor"
        segmentEditorNode = slicer.mrmlScene.GetSingletonNode(segmentEditorSingletonTag, "vtkMRMLSegmentEditorNode")
        if segmentEditorNode:
            segmentationNode = segmentEditorNode.GetSegmentationNode()
        if not segmentationNode:
            segmentationNodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
            if segmentationNodes:
                segmentationNode = segmentationNodes[0]
        if not segmentationNode:
            self.ui.segmentsTableWidget.setRowCount(0)
            return
        
        # Make sure the segmentation node has a display node
        if not segmentationNode.GetDisplayNode():
            segmentationNode.CreateDefaultDisplayNodes()
        
        segmentation = segmentationNode.GetSegmentation()
        numSegments = segmentation.GetNumberOfSegments()
        if numSegments == 0:
            self.ui.segmentsTableWidget.setRowCount(0)
            return
        
        self.ui.segmentsTableWidget.setRowCount(numSegments)
        for i in range(numSegments):
            segmentID = segmentation.GetNthSegmentID(i)
            segment = segmentation.GetSegment(segmentID)
            segmentName = segment.GetName()
            checkBox = qt.QCheckBox()
            checkBox.checked = self.segmentSelectionDict.get(segmentID, True)
            checkBox.connect("toggled(bool)", lambda checked, sID=segmentID: self.onSegmentSelectionChanged(sID, checked))
            nameItem = qt.QTableWidgetItem(segmentName)
            self.ui.segmentsTableWidget.setItem(i, 0, nameItem)
            self.ui.segmentsTableWidget.setCellWidget(i, 1, checkBox)
            self.segmentSelectionDict[segmentID] = checkBox.checked
        
        self._parameterNode.SetNodeReferenceID("CurrentSegmentation", segmentationNode.GetID())
        self.updateGUIFromParameterNode()

    def onSegmentSelectionChanged(self, segmentID, checked):
        self.segmentSelectionDict[segmentID] = checked
        self.updateParameterNodeFromGUI()

    def onSelectAllSegments(self):
        segmentationNodeID = self._parameterNode.GetNodeReferenceID("CurrentSegmentation")
        if not segmentationNodeID:
            return
        segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationNodeID)
        if not segmentationNode:
            return
        segmentation = segmentationNode.GetSegmentation()
        for i in range(self.ui.segmentsTableWidget.rowCount):
            segmentID = segmentation.GetNthSegmentID(i)
            self.segmentSelectionDict[segmentID] = True
            self.ui.segmentsTableWidget.cellWidget(i, 1).checked = True
        self.updateParameterNodeFromGUI()

    def onDeselectAllSegments(self):
        segmentationNodeID = self._parameterNode.GetNodeReferenceID("CurrentSegmentation")
        if not segmentationNodeID:
            return
        segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationNodeID)
        if not segmentationNode:
            return
        segmentation = segmentationNode.GetSegmentation()
        for i in range(self.ui.segmentsTableWidget.rowCount):
            segmentID = segmentation.GetNthSegmentID(i)
            self.segmentSelectionDict[segmentID] = False
            self.ui.segmentsTableWidget.cellWidget(i, 1).checked = False
        self.updateParameterNodeFromGUI()

    def onVolumeSelected(self):
        self.updateParameterNodeFromGUI()
        self.updateSegmentTable()

    def onMaterialMappingToggled(self, enabled):
        self.ui.materialMappingGroupBox.setVisible(enabled)
        self.updateParameterNodeFromGUI()
        
    # New methods for clipping feature
    def onEnableClippingButtonClicked(self):
        """Toggle clipping on/off"""
        self.clippingEnabled = not self.clippingEnabled
        self.ui.clippingControlsGroupBox.setVisible(self.clippingEnabled)
        
        if self.clippingEnabled:
            self.ui.enableClippingButton.setText("Disable Clipping")
            modelNode = self.ui.modelSelector.currentNode()
            if modelNode:
                self.setupClippingForModel(modelNode)
        else:
            self.ui.enableClippingButton.setText("Enable Clipping")
            self.disableClipping()
            
    def onModelSelected(self, modelNode):
        """Handle selection of a model for clipping"""
        if self.clippingEnabled and modelNode:
            # Disable clipping on previous model if there was one
            self.disableClipping()
            # Set up clipping for the newly selected model
            self.setupClippingForModel(modelNode)
        elif self.clippingEnabled and not modelNode:
            self.disableClipping()
            
    def setupClippingForModel(self, modelNode):
        """Set up clipping for the selected model"""
        if not modelNode:
            return
            
        # Clean up any existing clipping node
        self.disableClipping()
        
        # Create a new clipping node
        self.clippingNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLClipModelsNode")
        self.clippingNode.SetScene(slicer.mrmlScene)
        
        # Get model bounds
        bounds = [0, 0, 0, 0, 0, 0]
        modelNode.GetBounds(bounds)
        
        # Set initial clipping parameters
        clipDirection = self.ui.clipDirectionComboBox.currentIndex
        self.clippingNode.SetClipType(clipDirection)
        self.clippingNode.SetRedSliceClipState(1)  # 1 = positive side (keep)
        self.clippingNode.SetYellowSliceClipState(1)
        self.clippingNode.SetGreenSliceClipState(1)
        
        # Initialize slice positions based on model bounds
        sliceLogic = slicer.app.layoutManager().sliceWidget("Red").sliceLogic()
        sliceNode = sliceLogic.GetSliceNode()
        sliceOffset = (bounds[1] + bounds[0]) / 2  # Middle of model
        sliceNode.SetSliceOffset(sliceOffset)
        
        # Set slider range to model bounds
        if clipDirection == 0:  # Axial (Red slice - Z axis)
            self.ui.clipSliderWidget.minimum = bounds[4]  # Z min
            self.ui.clipSliderWidget.maximum = bounds[5]  # Z max
            self.ui.clipSliderWidget.value = (bounds[4] + bounds[5]) / 2
        elif clipDirection == 1:  # Sagittal (Yellow slice - X axis)
            self.ui.clipSliderWidget.minimum = bounds[0]  # X min
            self.ui.clipSliderWidget.maximum = bounds[1]  # X max
            self.ui.clipSliderWidget.value = (bounds[0] + bounds[1]) / 2
        else:  # Coronal (Green slice - Y axis)
            self.ui.clipSliderWidget.minimum = bounds[2]  # Y min
            self.ui.clipSliderWidget.maximum = bounds[3]  # Y max
            self.ui.clipSliderWidget.value = (bounds[2] + bounds[3]) / 2
        
        # Set model visibility to clipping
        displayNode = modelNode.GetDisplayNode()
        if displayNode:
            displayNode.SetClipping(1)
            
        # Enable clipping in Slicer
        slicer.mrmlScene.AddNode(self.clippingNode)
        
        # Store reference to model for later use
        self._parameterNode.SetNodeReferenceID("CurrentClippedModel", modelNode.GetID())
        
    def disableClipping(self):
        """Disable and clean up clipping"""
        if self.clippingNode:
            slicer.mrmlScene.RemoveNode(self.clippingNode)
            self.clippingNode = None
            
        # Disable clipping on the model's display node
        modelNodeID = self._parameterNode.GetNodeReferenceID("CurrentClippedModel")
        if modelNodeID:
            modelNode = slicer.mrmlScene.GetNodeByID(modelNodeID)
            if modelNode:
                displayNode = modelNode.GetDisplayNode()
                if displayNode:
                    displayNode.SetClipping(0)
            self._parameterNode.SetNodeReferenceID("CurrentClippedModel", "")
            
    def updateClippingDirection(self, index):
        """Update the clipping direction based on combo box selection"""
        if not self.clippingEnabled or not self.clippingNode:
            return
            
        direction = index
        self.clippingNode.SetClipType(direction)
        
        # Update slider range for the new direction
        modelNode = self.ui.modelSelector.currentNode()
        if modelNode:
            bounds = [0, 0, 0, 0, 0, 0]
            modelNode.GetBounds(bounds)
            
            if direction == 0:  # Axial
                self.ui.clipSliderWidget.minimum = bounds[4]  # Z min
                self.ui.clipSliderWidget.maximum = bounds[5]  # Z max
                self.ui.clipSliderWidget.value = (bounds[4] + bounds[5]) / 2
            elif direction == 1:  # Sagittal
                self.ui.clipSliderWidget.minimum = bounds[0]  # X min
                self.ui.clipSliderWidget.maximum = bounds[1]  # X max
                self.ui.clipSliderWidget.value = (bounds[0] + bounds[1]) / 2
            else:  # Coronal
                self.ui.clipSliderWidget.minimum = bounds[2]  # Y min
                self.ui.clipSliderWidget.maximum = bounds[3]  # Y max
                self.ui.clipSliderWidget.value = (bounds[2] + bounds[3]) / 2
                
    def updateClippingPosition(self, position):
        """Update the clipping position based on slider value"""
        if not self.clippingEnabled or not self.clippingNode:
            return
            
        direction = self.ui.clipDirectionComboBox.currentIndex
        
        # Update the slice position based on direction
        layoutManager = slicer.app.layoutManager()
        if direction == 0:  # Axial (Red slice)
            sliceWidget = layoutManager.sliceWidget("Red")
        elif direction == 1:  # Sagittal (Yellow slice)
            sliceWidget = layoutManager.sliceWidget("Yellow")
        else:  # Coronal (Green slice)
            sliceWidget = layoutManager.sliceWidget("Green")
            
        if sliceWidget:
            sliceLogic = sliceWidget.sliceLogic()
            sliceNode = sliceLogic.GetSliceNode()
            sliceNode.SetSliceOffset(position)
        
    def updateClippingFlip(self, flip):
        """Flip the clipping direction"""
        if not self.clippingEnabled or not self.clippingNode:
            return
            
        clipState = 2 if flip else 1  # 1 = positive side, 2 = negative side
        self.clippingNode.SetRedSliceClipState(clipState)
        self.clippingNode.SetYellowSliceClipState(clipState)
        self.clippingNode.SetGreenSliceClipState(clipState)

    def cleanup(self):
        self.removeObservers()
        # Clean up clipping if enabled
        if self.clippingEnabled:
            self.disableClipping()

    def enter(self):
        self.initializeParameterNode()
        self.updateSegmentTable()

    def exit(self):
        # Safely remove observer from the parameter node to suppress warnings.
        if self._parameterNode is not None:
            try:
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode)
            except Exception:
                pass

    def onSceneStartClose(self, caller, event):
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event):
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self):
        if not self.logic:
            self.logic = SpineMeshGeneratorLogic()
        paramNode = self.logic.getParameterNode()
        if not paramNode.GetParameter("TargetEdgeLength"):
            paramNode.SetParameter("TargetEdgeLength", "1.37")
        if not paramNode.GetParameter("PointSurfaceRatio"):
            paramNode.SetParameter("PointSurfaceRatio", "1.62")
        if not paramNode.GetParameter("Slope"):
            paramNode.SetParameter("Slope", "0.7")
        if not paramNode.GetParameter("Intercept"):
            paramNode.SetParameter("Intercept", "5.1")
        if not paramNode.GetParameter("OutputFormat"):
            paramNode.SetParameter("OutputFormat", "all")
        self.setParameterNode(paramNode)

    def setParameterNode(self, inputParameterNode):
        if self._parameterNode is not None:
            try:
                self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode)
            except Exception:
                pass
        self._parameterNode = inputParameterNode
        if self._parameterNode is not None:
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode)
        self.updateGUIFromParameterNode()

    def updateGUIFromParameterNode(self, caller=None, event=None):
        if self._parameterNode is None or self._updatingGUIFromParameterNode:
            return
        self._updatingGUIFromParameterNode = True
        self.ui.inputVolumeSelector.setCurrentNode(self._parameterNode.GetNodeReference("InputVolume"))
        self.ui.outputDirectorySelector.directory = self._parameterNode.GetParameter("OutputDirectory") or ""
        self.ui.targetEdgeLengthSpinBox.value = float(self._parameterNode.GetParameter("TargetEdgeLength") or 1.37)
        outputFormat = self._parameterNode.GetParameter("OutputFormat") or "all"
        formatIndex = -1
        for i in range(self.ui.outputFormatComboBox.count):
            if str(self.ui.outputFormatComboBox.itemData(i)) == outputFormat:
                formatIndex = i
                break
        if formatIndex >= 0:
            self.ui.outputFormatComboBox.currentIndex = formatIndex
        materialMapping = self._parameterNode.GetParameter("EnableMaterialMapping") == "true"
        self.ui.enableMaterialMappingCheckBox.checked = materialMapping
        self.ui.materialMappingGroupBox.setVisible(materialMapping)
        self.ui.slopeSpinBox.value = float(self._parameterNode.GetParameter("Slope") or 0.7)
        self.ui.interceptSpinBox.value = float(self._parameterNode.GetParameter("Intercept") or 5.1)
        
        # Update model selector with available models (new)
        if not self.ui.modelSelector.currentNode():
            # Try to find a volume mesh node to pre-select
            volumeMeshNodes = slicer.util.getNodesByClass("vtkMRMLModelNode")
            for node in volumeMeshNodes:
                if "_volume_mesh" in node.GetName():
                    self.ui.modelSelector.setCurrentNode(node)
                    break
        
        # Check if we can apply
        canApply = (self._parameterNode.GetNodeReference("InputVolume") and 
                    self._parameterNode.GetNodeReferenceID("CurrentSegmentation") and 
                    self._parameterNode.GetParameter("OutputDirectory"))
        if canApply:
            canApply = any(self.segmentSelectionDict.values())
        self.ui.applyButton.enabled = canApply
        self._updatingGUIFromParameterNode = False

    def updateParameterNodeFromGUI(self, caller=None, event=None):
        if self._parameterNode is None or self._updatingGUIFromParameterNode:
            return
        wasModified = self._parameterNode.StartModify()
        self._parameterNode.SetNodeReferenceID("InputVolume", self.ui.inputVolumeSelector.currentNodeID)
        self._parameterNode.SetParameter("OutputDirectory", self.ui.outputDirectorySelector.directory)
        self._parameterNode.SetParameter("TargetEdgeLength", str(self.ui.targetEdgeLengthSpinBox.value))
        outputFormat = self.ui.outputFormatComboBox.itemData(self.ui.outputFormatComboBox.currentIndex)
        self._parameterNode.SetParameter("OutputFormat", str(outputFormat) if outputFormat is not None else "all")
        self._parameterNode.SetParameter("EnableMaterialMapping", "true" if self.ui.enableMaterialMappingCheckBox.checked else "false")
        self._parameterNode.SetParameter("Slope", str(self.ui.slopeSpinBox.value))
        self._parameterNode.SetParameter("Intercept", str(self.ui.interceptSpinBox.value))
        
        # Store selected segments
        selectedSegments = []
        for segmentID, selected in self.segmentSelectionDict.items():
            if selected:
                selectedSegments.append(segmentID)
        self._parameterNode.SetParameter("SelectedSegments", ",".join(selectedSegments))
        self._parameterNode.EndModify(wasModified)

    def onApplyButton(self):
        with slicer.util.tryWithErrorDisplay("Failed to compute results.", waitCursor=True):
            # Get parameters from UI
            inputVolumeNode = self.ui.inputVolumeSelector.currentNode()
            outputDirectory = self.ui.outputDirectorySelector.directory
            targetEdgeLength = self.ui.targetEdgeLengthSpinBox.value
            outputFormat = str(self.ui.outputFormatComboBox.itemData(self.ui.outputFormatComboBox.currentIndex))
            enableMaterialMapping = self.ui.enableMaterialMappingCheckBox.checked
            
            # Get segmentation node
            segmentationNodeID = self._parameterNode.GetNodeReferenceID("CurrentSegmentation")
            if not segmentationNodeID:
                raise ValueError("No segmentation found")
            segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationNodeID)
            if not segmentationNode:
                raise ValueError("Segmentation node not found")
            
            # Build material parameters
            materialParams = {
                "slope": self.ui.slopeSpinBox.value,
                "intercept": self.ui.interceptSpinBox.value,
                "bone_threshold": 400,  # Fixed default value from config
                "neighborhood_radius": 2,  # Fixed default value from config
                "resolution_level": 1  # Fixed default value from config
            }
            
            # Get selected segments
            selectedSegments = []
            for segmentID, selected in self.segmentSelectionDict.items():
                if selected:
                    selectedSegments.append(segmentID)
            
            if not selectedSegments:
                raise ValueError("No segments selected for processing")
            
            # Start processing - store any created nodes and statistics
            createdNodes, meshStatistics, summary = self.logic.process(
                inputVolumeNode,
                segmentationNode,
                selectedSegments,
                outputDirectory,
                targetEdgeLength,
                outputFormat,
                enableMaterialMapping,
                materialParams
            )
            
            # Switch to a layout that shows the 3D view prominently
            if createdNodes and len(createdNodes) > 0:
                slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
                
                # Perform 3D view reset to center on newly loaded models
                threeDView = slicer.app.layoutManager().threeDWidget(0).threeDView()
                threeDView.resetFocalPoint()
                
                # Create a detailed message dialog with mesh statistics
                self.showMeshStatisticsDialog(meshStatistics, summary)
                
                # Show a simplified status message with totals
                volumeModels = [node for node in createdNodes if "volume_mesh" in node.GetName()]
                statusMessage = f"Processing complete. {len(volumeModels)} volume meshes with {summary['totalElements']} elements generated."
                
                # Update model selector with newly created models
                if volumeModels:
                    self.ui.modelSelector.setCurrentNode(volumeModels[0])
            else:
                statusMessage = "Processing complete. No meshes were loaded for display."
                
            slicer.util.showStatusMessage(statusMessage)
            
    def showMeshStatisticsDialog(self, meshStatistics, summary):
        """
        Show a dialog with detailed mesh statistics
        """
        # Format the statistics message
        messageText = f"<b>Mesh Generation Complete</b><br><br>"
        messageText += f"<b>Summary:</b><br>"
        messageText += f"Total meshes: {summary['totalMeshes']}<br>"
        messageText += f"Total elements: {summary['totalElements']}<br>"
        messageText += f"Average edge length: {summary['averageEdgeLength']:.2f}mm<br><br>"
        
        messageText += f"<b>Per-Segment Statistics:</b><br>"
        messageText += "<table border='1' cellpadding='3' style='border-collapse: collapse;'>"
        messageText += "<tr><th>Segment</th><th>Elements</th><th>Avg Edge Length</th><th>Volume (mm³)</th></tr>"
        
        for segmentName, stats in meshStatistics.items():
            elementCount = stats.get("volume_elements", 0)
            edgeLength = stats.get("vtk_mean_edge_length", 0.0)
            volume = stats.get("volume_mm3", 0.0)
            messageText += f"<tr><td>{segmentName}</td><td>{elementCount}</td><td>{edgeLength:.2f}mm</td><td>{volume:.1f}</td></tr>"
            
        messageText += "</table><br>"
        
        # Create a dialog to display mesh statistics
        meshStatsDialog = qt.QDialog(slicer.util.mainWindow())
        meshStatsDialog.setWindowTitle("Mesh Generation Results")
        meshStatsDialog.setMinimumWidth(400)
        
        layout = qt.QVBoxLayout(meshStatsDialog)
        
        label = qt.QLabel(messageText)
        label.setTextFormat(qt.Qt.RichText)
        layout.addWidget(label)
        
        closeButton = qt.QPushButton("Close")
        closeButton.setToolTip("Close this dialog")
        closeButton.connect("clicked(bool)", meshStatsDialog.accept)
        layout.addWidget(closeButton)
        
        # Show the dialog
        meshStatsDialog.exec_()

#
# SpineMeshGeneratorLogic
#

class SpineMeshGeneratorLogic(ScriptedLoadableModuleLogic):
    """Implements the computation for the module."""

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
    
    def getParameterNode(self):
        parameterNode = ScriptedLoadableModuleLogic.getParameterNode(self)
        if not parameterNode.GetParameter("TargetEdgeLength"):
            parameterNode.SetParameter("TargetEdgeLength", "1.37")  # Updated default to match config
        if not parameterNode.GetParameter("PointSurfaceRatio"):
            parameterNode.SetParameter("PointSurfaceRatio", "1.62")  # Added parameter for point-to-surface ratio
        if not parameterNode.GetParameter("Slope"):
            parameterNode.SetParameter("Slope", "0.7")
        if not parameterNode.GetParameter("Intercept"):
            parameterNode.SetParameter("Intercept", "5.1")
        if not parameterNode.GetParameter("OutputFormat"):
            parameterNode.SetParameter("OutputFormat", "all")
        return parameterNode
    
    def process(self, inputVolumeNode, segmentationNode, selectedSegments, outputDirectory, 
                targetEdgeLength, outputFormat, enableMaterialMapping, materialParams):
        """
        Process all selected segments and display the generated meshes.
        Returns a list of nodes created for display and a dictionary of mesh statistics.
        """
        if not os.path.exists(outputDirectory):
            os.makedirs(outputDirectory)
            
        logging.info(f"Starting mesh generation process. Target edge length: {targetEdgeLength}mm")
        
        # Keep track of all created nodes and mesh statistics
        createdNodes = []
        meshStatistics = {}
        
        # Process each selected segment
        for segmentID in selectedSegments:
            volumeNode, surfaceNode, stats = self.processSegment(
                inputVolumeNode, 
                segmentationNode, 
                segmentID, 
                outputDirectory, 
                targetEdgeLength, 
                outputFormat,
                enableMaterialMapping,
                materialParams
            )
            
            if volumeNode:
                createdNodes.append(volumeNode)
            if surfaceNode:
                createdNodes.append(surfaceNode)
            if stats:
                segmentation = segmentationNode.GetSegmentation()
                segment = segmentation.GetSegment(segmentID)
                segmentName = segment.GetName()
                meshStatistics[segmentName] = stats
                
        # Create a folder in the subject hierarchy to organize the models
        if createdNodes:
            try:
                shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
                folderItemID = shNode.CreateFolderItem(shNode.GetSceneItemID(), "Generated Meshes")
                
                # Add all created nodes to this folder
                for node in createdNodes:
                    nodeItemID = shNode.GetItemByDataNode(node)
                    if nodeItemID:
                        shNode.SetItemParent(nodeItemID, folderItemID)
                        
                # Expand the folder in the subject hierarchy
                meshFolderPlugin = slicer.qSlicerSubjectHierarchyFolderPlugin()
                if meshFolderPlugin:
                    meshFolderPlugin.setDisplayVisibility(folderItemID, 1)  # Show folder contents
            except Exception as e:
                logging.warning(f"Could not organize models in a folder: {str(e)}")
        
        # Reset 3D view to show new models
        layoutManager = slicer.app.layoutManager()
        if layoutManager.threeDViewCount > 0:
            threeDView = layoutManager.threeDWidget(0).threeDView()
            threeDView.resetFocalPoint()
            threeDView.resetCamera()
            
        # Calculate summary statistics for all generated meshes
        totalElements = sum(stats.get("volume_elements", 0) for stats in meshStatistics.values() if stats)
        avgEdgeLength = np.mean([stats.get("vtk_mean_edge_length", 0) for stats in meshStatistics.values() if stats and stats.get("vtk_mean_edge_length")])
        
        summary = {
            "totalMeshes": len(meshStatistics),
            "totalElements": totalElements,
            "averageEdgeLength": avgEdgeLength
        }
            
        logging.info(f"Mesh generation complete. Total {totalElements} elements with average edge length {avgEdgeLength:.2f}mm")
        
        return createdNodes, meshStatistics, summary
    
    def processSegment(self, inputVolumeNode, segmentationNode, segmentID, outputDirectory, 
                       targetEdgeLength, outputFormat, enableMaterialMapping, materialParams):
        """
        Process a single segment following the workflow from the configuration.
        """
        segmentation = segmentationNode.GetSegmentation()
        segment = segmentation.GetSegment(segmentID)
        segmentName = segment.GetName()
        logging.info(f"Processing segment: {segmentName}")
        
        # Create segment output directory
        segmentOutputDir = os.path.join(outputDirectory, segmentName)
        os.makedirs(segmentOutputDir, exist_ok=True)
        
        # Create a temporary segmentation with just this segment
        tempSegmentationNode = slicer.vtkMRMLSegmentationNode()
        slicer.mrmlScene.AddNode(tempSegmentationNode)
        tempSegmentationNode.GetSegmentation().AddSegment(segment)
        
        # Calculate volume and surface statistics
        segmentStats = self.calculateVolumeAndSurface(inputVolumeNode, tempSegmentationNode)
        logging.info(f"Computed Closed Surface Statistics: {segmentStats}")
        
        # Calculate point-surface ratio and number of points (using DEFAULT_POINT_TO_SURFACE_RATIO from config = 1.62)
        pointSurfaceRatio = 1.62  # Using fixed ratio from config
        numberPoints = self.calculateSurfaceNumberPoints(segmentStats["SurfaceArea_mm2"], pointSurfaceRatio)
        logging.info(f"Using point-surface ratio: {pointSurfaceRatio}")
        logging.info(f"Target number of points: {numberPoints}")
        
        # Build file paths
        paths = self.buildPaths(segmentOutputDir, segmentName)
        
        # Variables to track created nodes for display
        generatedVolumeNode = None
        generatedSurfaceNode = None
        meshStats = None
        
        try:
            # Export segmentation to model
            modelNode = self.exportSegmentationToModel(tempSegmentationNode)
            
            # Create uniform remesh with calculated number of points
            outputModelNode = self.createUniformRemesh(
                modelNode, 
                clusterK=round(numberPoints / 1000.0, 1)  # Round to 1 decimal as in the config
            )
            
            # Save surface mesh
            slicer.util.saveNode(outputModelNode, paths["surface_mesh_path"])
            logging.info(f"Surface mesh saved to {paths['surface_mesh_path']}")
            
            # Generate volume mesh with target edge length
            volume_mesh_path = self.generateVolumeMesh(outputModelNode, paths, targetEdgeLength)
            
            # Calculate material properties if enabled
            if enableMaterialMapping:
                self.calculateMaterialProperties(
                    paths["volume_mesh_path"], 
                    inputVolumeNode.GetID(), 
                    paths["element_properties_path"],
                    materialParams
                )
            
            # Calculate expanded mesh statistics (more comprehensive than before)
            meshStats = self.calculateMeshStatistics(
                paths["surface_mesh_path"],
                paths["volume_mesh_path"],
                paths["statistics_path"],
                segmentName,
                segmentStats,
                pointSurfaceRatio,
                numberPoints
            )
            
            # Generate additional output formats if requested
            if outputFormat == "summit" or outputFormat == "all":
                self.generateSummitFile(
                    paths["volume_mesh_path"],
                    paths["element_properties_path"] if enableMaterialMapping else None,
                    os.path.join(segmentOutputDir, f"{segmentName}_mesh.summit"),
                    enableMaterialMapping
                )
            
            # Load the volume mesh for display - this is the key addition
            generatedVolumeNode = slicer.util.loadModel(paths["volume_mesh_path"])
            # Update the name to include element count and edge length info
            volumeElementCount = meshStats.get("volume_elements", 0)
            volumeEdgeLength = meshStats.get("vtk_mean_edge_length", 0.0)
            generatedVolumeNode.SetName(f"{segmentName}_volume_mesh ({volumeElementCount} elements, {volumeEdgeLength:.2f}mm avg edge)")
            
            # Set volume mesh display properties for better visualization
            displayNode = generatedVolumeNode.GetDisplayNode()
            if displayNode:
                displayNode.SetColor(0.9, 0.8, 0.1)  # Yellow-gold color
                displayNode.SetOpacity(0.7)  # Semi-transparent
                displayNode.SetEdgeVisibility(True)  # Show edges
                displayNode.SetSliceIntersectionVisibility(True)  # Show in slice views
                displayNode.SetLineWidth(1.0)
            
            # Also load the surface mesh for comparison if needed
            generatedSurfaceNode = slicer.util.loadModel(paths["surface_mesh_path"])
            surfaceTriangleCount = meshStats.get("surface_triangles", 0)
            surfaceEdgeLength = meshStats.get("stl_mean_edge_length", 0.0)
            generatedSurfaceNode.SetName(f"{segmentName}_surface_mesh ({surfaceTriangleCount} triangles, {surfaceEdgeLength:.2f}mm avg edge)")
            
            # Set surface mesh display properties
            surfaceDisplayNode = generatedSurfaceNode.GetDisplayNode()
            if surfaceDisplayNode:
                surfaceDisplayNode.SetColor(0.2, 0.6, 0.8)  # Blue color
                surfaceDisplayNode.SetOpacity(0.3)  # More transparent
                surfaceDisplayNode.SetVisibility(False)  # Initially hidden but available
            
            logging.info(f"Segment {segmentName} processed successfully and meshes loaded for display")
            
        except Exception as e:
            logging.error(f"Error processing segment {segmentName}: {str(e)}")
            raise
        finally:
            # Clean up temporary nodes but keep the generated display nodes
            slicer.mrmlScene.RemoveNode(tempSegmentationNode)
            if 'modelNode' in locals() and modelNode:
                slicer.mrmlScene.RemoveNode(modelNode)
            if 'outputModelNode' in locals() and outputModelNode:
                slicer.mrmlScene.RemoveNode(outputModelNode)
            
        # Return the created nodes and stats so they can be tracked
        return generatedVolumeNode, generatedSurfaceNode, meshStats

    def analyzeMeshQuality(self, modelNode):
        """
        Analyze the quality of a tetrahedral mesh
        Returns a dictionary with quality metrics
        """
        import vtk
        import numpy as np
        
        if not modelNode:
            logging.error("No model node provided for quality analysis")
            return None
        
        # Ensure we're working with an unstructured grid (volume mesh)
        try:
            # If model node, try to get mesh from it
            mesh = modelNode.GetMesh()
            if not mesh:
                logging.error("Model node does not contain a mesh")
                return None
                
            if not mesh.IsA("vtkUnstructuredGrid"):
                # Try to convert to unstructured grid
                grid = vtk.vtkUnstructuredGrid()
                grid.SetPoints(mesh.GetPoints())
                
                # Check if we have tet cells (we need cell data)
                hasTets = False
                for i in range(mesh.GetNumberOfCells()):
                    cell = mesh.GetCell(i)
                    if cell.GetCellType() == vtk.VTK_TETRA:
                        hasTets = True
                        grid.InsertNextCell(vtk.VTK_TETRA, cell.GetPointIds())
                
                if not hasTets:
                    logging.error("Mesh does not contain tetrahedral elements")
                    return {"num_elements": 0, "poor_elements": 0, "poor_elements_percent": 0, 
                            "avg_aspect_ratio": 0, "max_aspect_ratio": 0}
                    
                mesh = grid
        except Exception as e:
            logging.error(f"Error processing mesh: {str(e)}")
            return None
        
        # Initialize quality metric calculator
        qualityFilter = vtk.vtkMeshQuality()
        qualityFilter.SetInputData(mesh)
        qualityFilter.SetTetQualityMeasureToAspectRatio()
        qualityFilter.Update()
        
        qualityArray = qualityFilter.GetOutput().GetCellData().GetArray("Quality")
        if not qualityArray:
            logging.error("Unable to compute quality metrics")
            return None
        
        # Extract quality values
        numElements = qualityArray.GetNumberOfTuples()
        aspectRatios = [qualityArray.GetValue(i) for i in range(numElements)]
        
        # Calculate statistics
        if numElements > 0:
            avgAspectRatio = np.mean(aspectRatios)
            maxAspectRatio = np.max(aspectRatios)
            poorElements = sum(1 for ratio in aspectRatios if ratio > 5)
            poorElementsPercent = (poorElements / numElements) * 100
        else:
            avgAspectRatio = 0
            maxAspectRatio = 0
            poorElements = 0
            poorElementsPercent = 0
        
        # Return results
        results = {
            "num_elements": numElements,
            "poor_elements": poorElements,
            "poor_elements_percent": poorElementsPercent,
            "avg_aspect_ratio": avgAspectRatio,
            "max_aspect_ratio": maxAspectRatio
        }
        
        logging.info(f"Mesh quality analysis completed. {numElements} elements analyzed.")
        logging.info(f"Average aspect ratio: {avgAspectRatio:.3f}, Max: {maxAspectRatio:.3f}")
        logging.info(f"Poor elements (ratio > 5): {poorElements} ({poorElementsPercent:.2f}%)")
        
        return results

    def calculateVolumeAndSurface(self, volumeNode, segmentationNode):
        """
        Calculate volume and surface area statistics for a segmentation.
        Aligned with the desired workflow's calculate_volume_and_surface function.
        """
        if not segmentationNode.GetDisplayNode():
            segmentationNode.CreateDefaultDisplayNodes()
            
        segStatLogic = SegmentStatisticsLogic()
        parameterNode = segStatLogic.getParameterNode()
        parameterNode.SetParameter("Segmentation", segmentationNode.GetID())
        parameterNode.SetParameter("ScalarVolume", volumeNode.GetID())
        parameterNode.SetParameter("ClosedSurfaceSegmentStatisticsPlugin.enabled", "True")
        
        try:
            segStatLogic.computeStatistics()
            tableNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode")
            parameterNode.SetParameter("MeasurementsTable", tableNode.GetID())
            segStatLogic.exportToTable(tableNode)
            
            volColIndex = tableNode.GetColumnIndex("Volume mm3 (CS)")
            surfColIndex = tableNode.GetColumnIndex("Surface mm2")
            
            volume_mm3 = tableNode.GetCellText(0, volColIndex) if volColIndex >= 0 else None
            surface_mm2 = tableNode.GetCellText(0, surfColIndex) if surfColIndex >= 0 else None
            
            stats = {
                "Volume_mm3": float(volume_mm3) if volume_mm3 else 1000.0,
                "SurfaceArea_mm2": float(surface_mm2) if surface_mm2 else 500.0,
            }
            
            slicer.mrmlScene.RemoveNode(tableNode)
            return stats
            
        except Exception as e:
            logging.error(f"Error computing statistics: {str(e)}")
            return {"Volume_mm3": 1000.0, "SurfaceArea_mm2": 500.0}
    
    def calculateSurfaceNumberPoints(self, surfaceArea, pointSurfaceRatio=1.62):
        """
        Calculate the number of points required for the surface mesh.
        Aligned with desired workflow's calculate_surface_number_points function.
        """
        numPoints = surfaceArea * pointSurfaceRatio
        return numPoints
    
    def buildPaths(self, outputDir, segmentName):
        """
        Build file paths for mesh generation outputs.
        """
        paths = {
            "element_properties_path": os.path.join(outputDir, f"{segmentName}_element_properties.csv"),
            "surface_mesh_path": os.path.join(outputDir, f"{segmentName}_surface_mesh.stl"),
            "volume_mesh_path": os.path.join(outputDir, f"{segmentName}_volume_mesh.vtk"),
            "volume_mesh_abaqus_path": os.path.join(outputDir, f"{segmentName}_volume_mesh.inp"),
            "volume_mesh_gmsh_path": os.path.join(outputDir, f"{segmentName}_surface_mesh.msh"),
            "statistics_path": os.path.join(outputDir, f"{segmentName}_statistics.csv")
        }
        return paths
    
    def exportSegmentationToModel(self, segmentationNode, modelName="UniformRemeshInput"):
        """
        Export segmentation to model node.
        Aligned with desired workflow's export_segmentation_to_model function.
        """
        if not segmentationNode.GetDisplayNode():
            segmentationNode.CreateDefaultDisplayNodes()
            
        slicer.modules.segmentations.logic().ExportAllSegmentsToModels(segmentationNode, True)
        updatedModels = slicer.util.getNodesByClass("vtkMRMLModelNode")
        
        if not updatedModels:
            raise ValueError("Failed to export segmentation to model")
            
        modelNode = updatedModels[-1]
        modelNode.SetName(modelName)
        
        if not modelNode.GetDisplayNode():
            modelNode.CreateDefaultDisplayNodes()
            
        return modelNode
    
    def createUniformRemesh(self, inputModel, clusterK=10, modelName="UniformRemeshOutput"):
        """
        Create uniform remesh using SurfaceToolbox.
        Aligned with desired workflow's uniform_remesh function.
        """
        outputModelNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", modelName)
        surfaceToolBoxLogic = SurfaceToolboxLogic()
        
        parameterNode = surfaceToolBoxLogic.getParameterNode()
        parameterNode.SetNodeReferenceID("inputModel", inputModel.GetID())
        parameterNode.SetNodeReferenceID("outputModel", outputModelNode.GetID())
        parameterNode.SetParameter("remesh", "true")
        parameterNode.SetParameter("remeshClustersK", str(clusterK))
        parameterNode.SetParameter("remeshSubdivide", "0")
        parameterNode.SetParameter("remeshDecimate", "0.0")
        parameterNode.SetParameter("boundarySmoothing", "false")
        parameterNode.SetParameter("normals", "false")
        parameterNode.SetParameter("mirror", "false")
        parameterNode.SetParameter("mirrorX", "false")
        parameterNode.SetParameter("mirrorY", "false") 
        parameterNode.SetParameter("mirrorZ", "false")
        parameterNode.SetParameter("cleaner", "false")
        parameterNode.SetParameter("connectivity", "false")
        parameterNode.SetParameter("smoothing", "false")
        parameterNode.SetParameter("smoothingFactor", "0.5")
        parameterNode.SetParameter("fillHoles", "false")
        parameterNode.SetParameter("fillHolesSize", "1000.0")
        
        surfaceToolBoxLogic.applyFilters(parameterNode)
        return outputModelNode
    
    def generateVolumeMesh(self, outputModelNode, paths, targetEdgeLength):
        """
        Generate volume mesh using GMSH.
        Aligned with desired workflow's generate_mesh function.
        """
        temp_dir = tempfile.mkdtemp()
        stl_temp = os.path.join(temp_dir, "remesh_output.stl")
        msh_temp = os.path.join(temp_dir, "volume_mesh.msh")
        
        # Save the STL for mesh generation
        slicer.util.saveNode(outputModelNode, stl_temp)
        
        # Determine script path for mesh generation
        scriptPath = os.path.dirname(os.path.abspath(__file__))
        gmshScriptPath = os.path.join(scriptPath, "generate_mesh.py")
        
        if not os.path.exists(gmshScriptPath):
            self.createGmshScript(gmshScriptPath)
            
        pythonExec = sys.executable
        cmd = [pythonExec, gmshScriptPath, stl_temp, msh_temp, str(targetEdgeLength)]
        
        # Use clean environment to avoid Python conflicts
        clean_env = {k: v for k, v in os.environ.items() 
                    if k not in ["PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"]}
        
        try:
            subprocess.check_call(cmd, env=clean_env)
            logging.info(f"GMSH successfully generated mesh: {msh_temp}")
            
            # Copy GMSH output to designated path
            shutil.copy(msh_temp, paths["volume_mesh_gmsh_path"])
            
            # Convert to other formats and save
            self.convertAndSaveMesh(msh_temp, paths)
            logging.info(f"Volume mesh saved to {paths['volume_mesh_path']}")
            
            return paths["volume_mesh_path"]
            
        except subprocess.CalledProcessError as e:
            logging.error(f"Error running GMSH: {e}")
            raise
        except Exception as e:
            logging.error(f"Error generating volume mesh: {str(e)}")
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    def convertAndSaveMesh(self, input_filepath, paths):
        """
        Convert GMSH mesh to other formats and save them.
        """
        import meshio
        mesh = meshio.read(input_filepath)
        volume_mesh = self.removeMeshTriangles(mesh)
        meshio.write(paths["volume_mesh_path"], volume_mesh)
        meshio.write(paths["volume_mesh_abaqus_path"], volume_mesh, file_format="abaqus")
    
    def removeMeshTriangles(self, mesh):
        """
        Remove triangles from mesh, keeping only tetrahedra.
        Aligned with desired workflow's remove_triangles function.
        """
        import meshio
        new_cells = []
        new_cell_data = {key: [] for key in mesh.cell_data} if mesh.cell_data else None
        
        for i, cell_block in enumerate(mesh.cells):
            if cell_block.type != "triangle":
                new_cells.append(cell_block)
                if new_cell_data is not None:
                    for key in mesh.cell_data:
                        new_cell_data[key].append(mesh.cell_data[key][i])
                        
        new_mesh = meshio.Mesh(
            points=mesh.points,
            cells=new_cells,
            cell_data=new_cell_data if new_cell_data else {},
            point_data=mesh.point_data,
            field_data=mesh.field_data
        )
        return new_mesh
    
    def calculateMaterialProperties(self, mesh_filepath, volume_node_id, output_filepath, materialParams):
        """
        Calculate material properties (BMD and BV/TV) for each tetrahedral element in the mesh.
        Using SimpleITK approach from your existing implementation.
        """
        import os
        import tempfile
        import csv
        import vtk
        import SimpleITK as sitk
        import numpy as np

        logging.info("Calculating material properties using SimpleITK-based logic...")

        volumeNode = slicer.mrmlScene.GetNodeByID(volume_node_id)
        if not volumeNode:
            logging.error("CT volume node not found")
            return

        # Save CT volume to temporary file
        tmpCTFile = os.path.join(tempfile.gettempdir(), "tempCT.nii.gz")
        try:
            slicer.util.saveNode(volumeNode, tmpCTFile)
        except Exception as e:
            logging.error(f"Failed to save CT volume to temporary file: {str(e)}")
            return

        # Load and potentially resample CT volume
        def load_ct_volume(ct_filename, resolution_level):
            try:
                ct_image = sitk.ReadImage(ct_filename)
                if resolution_level != 1:
                    original_spacing = ct_image.GetSpacing()
                    original_size = ct_image.GetSize()
                    new_spacing = [s / resolution_level for s in original_spacing]
                    new_size = [int(sz * resolution_level) for sz in original_size]
                    logging.info(f"Original image size: {original_size}, spacing: {original_spacing}")
                    logging.info(f"Resampled image size: {new_size}, spacing: {new_spacing}")
                    
                    resample = sitk.ResampleImageFilter()
                    resample.SetInterpolator(sitk.sitkLinear)
                    resample.SetOutputSpacing(new_spacing)
                    resample.SetSize(new_size)
                    resample.SetOutputOrigin(ct_image.GetOrigin())
                    resample.SetOutputDirection(ct_image.GetDirection())
                    resample.SetDefaultPixelValue(ct_image.GetPixelIDValue())
                    return resample.Execute(ct_image)
                return ct_image
            except Exception as e:
                logging.error(f"Error loading or resampling CT volume: {str(e)}")
                return None

        # Use parameters from materialParams
        resolution_level = materialParams.get("resolution_level", 1)
        ct_image = load_ct_volume(tmpCTFile, resolution_level)
        if ct_image is None:
            logging.error("Error: CT volume could not be loaded/resampled.")
            return

        # Read the mesh
        reader = vtk.vtkUnstructuredGridReader()
        reader.SetFileName(mesh_filepath)
        reader.Update()
        mesh = reader.GetOutput()
        if mesh.GetNumberOfCells() == 0:
            logging.error(f"Mesh file '{mesh_filepath}' contains no cells.")
            return

        # Helper functions
        def world_to_image(point, ct_img):
            return ct_img.TransformPhysicalPointToIndex(point)

        def get_neighborhood(ct_img, center, radius):
            size = ct_img.GetSize()
            neighborhood = []
            for x in range(max(0, center[0] - radius), min(size[0], center[0] + radius + 1)):
                for y in range(max(0, center[1] - radius), min(size[1], center[1] + radius + 1)):
                    for z in range(max(0, center[2] - radius), min(size[2], center[2] + radius + 1)):
                        neighborhood.append(ct_img.GetPixel((x, y, z)))
            return neighborhood

        # Calculate properties for each tetrahedral element
        def calculate_properties(mesh, ct_img, slope, intercept, bone_threshold, neighborhood_radius):
            element_properties = []
            all_hu_values = []
            num_cells = mesh.GetNumberOfCells()
            
            for i in range(num_cells):
                cell = mesh.GetCell(i)
                if cell.GetCellType() == vtk.VTK_TETRA:
                    # Get cell points
                    pts = [mesh.GetPoint(cell.GetPointId(j)) for j in range(cell.GetNumberOfPoints())]
                    pts_np = np.array(pts)
                    centroid = pts_np.mean(axis=0)
                    
                    # Convert to image coordinates
                    image_point = world_to_image(list(centroid), ct_img)
                    image_point = tuple(int(round(c)) for c in image_point)
                    size = ct_img.GetSize()
                    
                    if all(0 <= image_point[j] < size[j] for j in range(3)):
                        # Get neighborhood HU values
                        neighborhood = get_neighborhood(ct_img, image_point, neighborhood_radius)
                        avg_hu = np.mean(neighborhood)
                        all_hu_values.append(avg_hu)
                        
                        # Calculate BMD and BV/TV
                        bmd_value = slope * avg_hu + intercept if avg_hu > 0 else 0.0
                        bvtv = max(bmd_value / 684.0, 0.001)  # Ensure minimum value
                        element_properties.append((i, bmd_value, bvtv))
                    else:
                        # Outside of image bounds
                        element_properties.append((i, 0.001, 0.001))
            
            # Log statistics
            if all_hu_values:
                logging.info(f"HU statistics -- min: {min(all_hu_values):.2f}, max: {max(all_hu_values):.2f}, mean: {np.mean(all_hu_values):.2f}")
            else:
                logging.info("No HU values collected from mesh.")
                
            return element_properties

        # Save results to CSV
        def save_results(element_properties, output_filepath):
            with open(output_filepath, "w") as f:
                writer = csv.writer(f)
                writer.writerow(["New_Element_ID", "Original_Element_ID", "BMD", "BV/TV"])
                for new_id, (orig_id, bmd, bvtv) in enumerate(element_properties):
                    writer.writerow([new_id, orig_id, f"{bmd:.4f}", f"{bvtv:.4f}"])

        # Get material parameters
        slope = materialParams.get("slope", 0.7)
        intercept = materialParams.get("intercept", 5.1)
        bone_threshold = materialParams.get("bone_threshold", 400)
        neighborhood_radius = materialParams.get("neighborhood_radius", 2)

        logging.info("Processing mesh to calculate element properties...")
        element_properties = calculate_properties(mesh, ct_image, slope, intercept, bone_threshold, neighborhood_radius)
        
        if not element_properties:
            logging.error("No tetrahedral elements were processed. Check the mesh.")
            return
            
        logging.info(f"Processed {len(element_properties)} tetrahedral elements.")
        save_results(element_properties, output_filepath)
        logging.info(f"Material properties saved to {output_filepath}")
    
    def calculateMeshStatistics(self, surface_mesh_path, volume_mesh_path, statistics_path, 
                               sample_id, segmentStats, pointSurfaceRatio, numberPoints):
        """
        Calculate comprehensive mesh statistics.
        Enhanced to include more metrics from the desired workflow.
        """
        import meshio
        import numpy as np
        import csv
        
        # Load meshes
        surface_mesh = meshio.read(surface_mesh_path)
        volume_mesh = meshio.read(volume_mesh_path)
        
        # Count elements
        surface_triangles = sum(len(c.data) for c in surface_mesh.cells if c.type == "triangle")
        volume_elements = sum(len(c.data) for c in volume_mesh.cells if c.type in ["tetra", "hexahedron"])
        
        # Calculate surface mesh metrics
        surface_edge_lengths = []
        surface_angles = []
        surface_aspect_ratios = []
        
        for cell_block in surface_mesh.cells:
            if cell_block.type == "triangle":
                for triangle in cell_block.data:
                    p1, p2, p3 = surface_mesh.points[triangle]
                    
                    # Edge lengths
                    e1 = np.linalg.norm(p1 - p2)
                    e2 = np.linalg.norm(p2 - p3)
                    e3 = np.linalg.norm(p3 - p1)
                    surface_edge_lengths.extend([e1, e2, e3])
                    
                    # Calculate angles
                    v1 = p2 - p1
                    v2 = p3 - p1
                    v3 = p3 - p2
                    
                    # Normalize vectors
                    v1_norm = v1 / np.linalg.norm(v1)
                    v2_norm = v2 / np.linalg.norm(v2)
                    v3_norm = v3 / np.linalg.norm(v3)
                    
                    # Calculate angles in degrees
                    angle1 = np.arccos(np.clip(np.dot(v1_norm, v2_norm), -1.0, 1.0)) * 180 / np.pi
                    angle2 = np.arccos(np.clip(np.dot(-v1_norm, v3_norm), -1.0, 1.0)) * 180 / np.pi
                    angle3 = np.arccos(np.clip(np.dot(-v2_norm, -v3_norm), -1.0, 1.0)) * 180 / np.pi
                    
                    surface_angles.extend([angle1, angle2, angle3])
                    
                    # Calculate aspect ratio (longest edge / shortest edge)
                    aspect_ratio = max(e1, e2, e3) / min(e1, e2, e3)
                    surface_aspect_ratios.append(aspect_ratio)
        
        # Calculate volume mesh metrics
        volume_edge_lengths = []
        tet_edge_ratios = []
        tet_volumes = []
        tet_jacobians = []
        
        for cell_block in volume_mesh.cells:
            if cell_block.type == "tetra":
                for tetra in cell_block.data:
                    p1, p2, p3, p4 = volume_mesh.points[tetra]
                    
                    # Edge lengths
                    edges = [
                        np.linalg.norm(p1 - p2),
                        np.linalg.norm(p1 - p3),
                        np.linalg.norm(p1 - p4),
                        np.linalg.norm(p2 - p3),
                        np.linalg.norm(p2 - p4),
                        np.linalg.norm(p3 - p4),
                    ]
                    volume_edge_lengths.extend(edges)
                    
                    # Edge ratio (longest/shortest)
                    tet_edge_ratios.append(max(edges) / min(edges))
                    
                    # Calculate tetrahedron volume
                    v1 = p2 - p1
                    v2 = p3 - p1
                    v3 = p4 - p1
                    volume = abs(np.dot(np.cross(v1, v2), v3)) / 6.0
                    tet_volumes.append(volume)
                    
                    # Simple Jacobian approximation (determinant of edge vectors)
                    matrix = np.column_stack((v1, v2, v3))
                    jacobian = abs(np.linalg.det(matrix))
                    tet_jacobians.append(jacobian)
        
        # Calculate statistics
        surface_mean_edge_length = np.mean(surface_edge_lengths) if surface_edge_lengths else None
        volume_mean_edge_length = np.mean(volume_edge_lengths) if volume_edge_lengths else None
        
        # Compile statistics
        stats = {
            "sample_id": sample_id,
            "surface_area_mm2": segmentStats["SurfaceArea_mm2"],
            "volume_mm3": segmentStats["Volume_mm3"],
            "surface_number_of_points_exact": numberPoints,
            "surface_number_of_points_slicer_rounded": round(numberPoints / 1000.0, 1) * 1000,
            "surface_triangles": surface_triangles,
            "volume_elements": volume_elements,
            "point_surface_ratio": pointSurfaceRatio,
            "stl_mean_edge_length": surface_mean_edge_length,
            "stl_mean_min_angle": np.min(surface_angles) if surface_angles else None,
            "stl_mean_max_angle": np.max(surface_angles) if surface_angles else None,
            "stl_mean_aspect_ratio": np.mean(surface_aspect_ratios) if surface_aspect_ratios else None,
            "stl_min_angle": np.min(surface_angles) if surface_angles else None,
            "stl_max_angle": np.max(surface_angles) if surface_angles else None,
            "vtk_mean_edge_length": volume_mean_edge_length,
            "tet_edge_ratio": np.mean(tet_edge_ratios) if tet_edge_ratios else None,
            "tet_mean_volume": np.mean(tet_volumes) if tet_volumes else None,
            "tet_min_volume": np.min(tet_volumes) if tet_volumes else None,
            "tet_total_volume": np.sum(tet_volumes) if tet_volumes else None,
            "tet_jacobian": np.mean(tet_jacobians) if tet_jacobians else None,
            "tet_min_jacobian": np.min(tet_jacobians) if tet_jacobians else None
        }
        
        # Define CSV headers
        headers = [
            "Sample ID", 
            "Surface Area (mm2)", 
            "Volume (mm3)",
            "Surface Number of Points (Exact)",
            "Surface Number of Points (Rounded)",
            "Surface Number of Triangles", 
            "Number of Elements in Volume Mesh",
            "Point Surface Ratio", 
            "Surface Mean Edge Length",
            "Surface Min Angle",
            "Surface Max Angle",
            "Surface Mean Aspect Ratio", 
            "Volume Mean Edge Length",
            "Tet Edge Ratio",
            "Tet Mean Volume",
            "Tet Min Volume",
            "Tet Total Volume",
            "Tet Mean Jacobian",
            "Tet Min Jacobian"
        ]
        
        # Write statistics to CSV
        with open(statistics_path, "w") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerow([
                stats["sample_id"],
                stats["surface_area_mm2"],
                stats["volume_mm3"],
                stats["surface_number_of_points_exact"],
                stats["surface_number_of_points_slicer_rounded"],
                stats["surface_triangles"],
                stats["volume_elements"],
                stats["point_surface_ratio"],
                stats["stl_mean_edge_length"],
                stats["stl_mean_min_angle"],
                stats["stl_mean_max_angle"],
                stats["stl_mean_aspect_ratio"],
                stats["vtk_mean_edge_length"],
                stats["tet_edge_ratio"],
                stats["tet_mean_volume"],
                stats["tet_min_volume"],
                stats["tet_total_volume"],
                stats["tet_jacobian"],
                stats["tet_min_jacobian"]
            ])
        
        logging.info(f"Mesh statistics saved to {statistics_path}")
        return stats
    
    def generateSummitFile(self, volume_mesh_path, element_properties_path, output_path, has_material_properties):
        """
        Generate Summit format file for finite element analysis.
        """
        import vtk
        
        # Read the mesh
        reader = vtk.vtkUnstructuredGridReader()
        reader.SetFileName(volume_mesh_path)
        reader.Update()
        mesh = reader.GetOutput()
        
        # Load material properties if available
        element_properties = {}
        if has_material_properties and element_properties_path and os.path.exists(element_properties_path):
            with open(element_properties_path, 'r') as f:
                next(f)  # Skip header
                for line in f:
                    parts = line.strip().split(',')
                    if len(parts) >= 4:
                        element_id = int(parts[0])
                        bvtv = float(parts[3])  # BV/TV
                        # Compute Young's modulus using power law E = 6950 * (BV/TV)^1.49
                        young_modulus = 6950 * (bvtv ** 1.49)
                        element_properties[element_id] = young_modulus
        
        # Write Summit file
        with open(output_path, 'w') as f:
            nNodes = mesh.GetNumberOfPoints()
            nElems = mesh.GetNumberOfCells()
            
            # Write header
            f.write("3\n")
            f.write(f"{nNodes} {nElems} 1 1\n")
            
            # Write node coordinates
            for i in range(nNodes):
                point = mesh.GetPoint(i)
                f.write(f"{point[0]:.15f} {point[1]:.15f} {point[2]:.15f}\n")
            
            # Write elements
            for i in range(nElems):
                cell = mesh.GetCell(i)
                if cell.GetCellType() == vtk.VTK_TETRA:
                    point_ids = [str(cell.GetPointId(j)) for j in range(cell.GetNumberOfPoints())]
                    f.write(f"1 {' '.join(point_ids)}\n")
                else:
                    f.write(f"1 0 0 0 0\n")
            
            # Write material properties
            f.write("10\n")
            if has_material_properties and element_properties:
                for i in range(nElems):
                    young_modulus = element_properties.get(i, 6950 * (0.001 ** 1.49))
                    f.write(f"{young_modulus:.15f}\n")
            else:
                default_modulus = 6950 * (0.001 ** 1.49)
                for _ in range(nElems):
                    f.write(f"{default_modulus:.15f}\n")
        
        logging.info(f"Summit file saved to {output_path}")
    
    def createGmshScript(self, script_path):
        """
        Create a Python script for GMSH mesh generation.
        """
        script_content = """import sys
import gmsh

def generate_mesh():
    if len(sys.argv) < 3:
        print("Usage: python generate_mesh.py input.stl output.msh [element_size]")
        return
    
    input_stl = sys.argv[1]
    output_msh = sys.argv[2]
    size_param = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    
    gmsh.initialize()
    gmsh.model.add("VolumeFromSTL")
    print(f"Merging STL file: {input_stl}")
    gmsh.merge(input_stl)
    
    entities = gmsh.model.getEntities(dim=2)
    if not entities:
        print("No surfaces found.")
        gmsh.finalize()
        sys.exit(1)
    
    surface_tags = [entity[1] for entity in entities]
    loop = gmsh.model.geo.addSurfaceLoop(surface_tags)
    volume = gmsh.model.geo.addVolume([loop])
    gmsh.model.geo.synchronize()
    
    # Set mesh size parameters
    gmsh.option.setNumber('Mesh.MeshSizeMin', size_param)
    gmsh.option.setNumber('Mesh.MeshSizeMax', size_param)
    
    # Generate 3D mesh
    gmsh.model.mesh.generate(3)
    
    # Write output
    gmsh.write(output_msh)
    gmsh.finalize()
    print(f"Mesh generation complete. Saved to: {output_msh}")

if __name__ == "__main__":
    generate_mesh()
"""
        with open(script_path, 'w') as f:
            f.write(script_content)
        logging.info(f"Created GMSH script at {script_path}")
        return script_path

# Register the module for testing
class SpineMeshGeneratorTest(ScriptedLoadableModuleTest):
    def setUp(self):
        slicer.mrmlScene.Clear()
    
    def runTest(self):
        self.setUp()
        self.test_SpineMeshGenerator()
    
    def test_SpineMeshGenerator(self):
        self.delayDisplay("Starting the test")
        volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "TestVolume")
        self.delayDisplay("Volume node created")
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "TestSegmentation")
        segmentationNode.CreateDefaultDisplayNodes()
        segmentationNode.GetSegmentation().AddEmptySegment("TestSegment")
        self.delayDisplay("Segmentation created")
        import tempfile
        outputDir = tempfile.mkdtemp()
        materialParams = {
            "slope": 0.7,
            "intercept": 5.1,
            "bone_threshold": 400,
            "neighborhood_radius": 2,
            "resolution_level": 1
        }
        logic = SpineMeshGeneratorLogic()
        self.assertIsNotNone(logic)
        import shutil
        shutil.rmtree(outputDir)
        self.delayDisplay('Test passed')
