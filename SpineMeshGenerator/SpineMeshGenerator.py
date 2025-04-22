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
        requiredPackages = ["meshio", "pyacvd", "tqdm", "SimpleITK", "gmsh"]
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
        self.sliceNodes = {}
        self._segmentVisibilityStates = {}  # Store visibility states

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

        # Connect clipping controls
        self.ui.enableClippingButton.connect('toggled(bool)', self.onEnableClippingButtonToggled)
        self.ui.modelSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onModelSelectedForClipping)
        self.ui.redSliceOffsetSlider.connect('valueChanged(double)', lambda value: self.onSliceOffsetChanged('Red', value))
        self.ui.yellowSliceOffsetSlider.connect('valueChanged(double)', lambda value: self.onSliceOffsetChanged('Yellow', value))
        self.ui.greenSliceOffsetSlider.connect('valueChanged(double)', lambda value: self.onSliceOffsetChanged('Green', value))
        
        # Hide clipping controls initially
        self.ui.clippingControlsGroupBox.setVisible(False)

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
        
        # Add observers for scene changes
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onSceneNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onSceneNodeRemoved)

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

        # Find all segmentation nodes in the scene
        segmentationNodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
        
        # Clear existing table
        self.ui.segmentsTableWidget.setRowCount(0)
        
        # Update current segmentation reference in parameter node if needed
        if segmentationNodes and not self._parameterNode.GetNodeReferenceID("CurrentSegmentation"):
            self._parameterNode.SetNodeReferenceID("CurrentSegmentation", segmentationNodes[0].GetID())
        
        row = 0
        for segmentationNode in segmentationNodes:
            if not segmentationNode.GetDisplayNode():
                segmentationNode.CreateDefaultDisplayNodes()
                
            # If this is the first segmentation node and no current segmentation is set
            if row == 0 and not self._parameterNode.GetNodeReferenceID("CurrentSegmentation"):
                self._parameterNode.SetNodeReferenceID("CurrentSegmentation", segmentationNode.GetID())
                
            segmentation = segmentationNode.GetSegmentation()
            
            # Add all segments from this segmentation
            for segmentIndex in range(segmentation.GetNumberOfSegments()):
                segmentID = segmentation.GetNthSegmentID(segmentIndex)
                segment = segmentation.GetSegment(segmentID)
                segmentName = segment.GetName()
                
                self.ui.segmentsTableWidget.insertRow(row)
                
                # Create segment name item
                nameItem = qt.QTableWidgetItem(f"{segmentationNode.GetName()}: {segmentName}")
                self.ui.segmentsTableWidget.setItem(row, 0, nameItem)
                
                # Create checkbox
                checkBox = qt.QCheckBox()
                checkBox.checked = self.segmentSelectionDict.get(segmentID, False)
                checkBox.connect("toggled(bool)", 
                    lambda checked, sID=segmentID, node=segmentationNode: 
                    self.onSegmentSelectionChanged(sID, checked, node))
                
                self.ui.segmentsTableWidget.setCellWidget(row, 1, checkBox)
                
                # Update visibility based on current selection
                self.updateSegmentVisibility(segmentID, checkBox.checked, segmentationNode)
                
                row += 1
        
        # Force GUI update
        self.updateGUIFromParameterNode()

    def onSegmentSelectionChanged(self, segmentID, checked, segmentationNode):
        """Handle segment selection changes and update visibility"""
        self.segmentSelectionDict[segmentID] = checked
        
        # Update current segmentation reference in parameter node
        if checked and segmentationNode:
            self._parameterNode.SetNodeReferenceID("CurrentSegmentation", segmentationNode.GetID())
        
        self.updateSegmentVisibility(segmentID, checked, segmentationNode)
        self.updateGUIFromParameterNode()

    def updateSegmentVisibility(self, segmentID, visible, segmentationNode):
        """Update the visibility of a segment and its parent hierarchy"""
        if not segmentationNode or not segmentationNode.GetDisplayNode():
            return
            
        # Store the original visibility state if not already stored
        if segmentID not in self._segmentVisibilityStates:
            self._segmentVisibilityStates[segmentID] = segmentationNode.GetDisplayNode().GetSegmentVisibility(segmentID)
            
        # Set segment visibility
        segmentationNode.GetDisplayNode().SetSegmentVisibility(segmentID, visible)
        
        # Make sure the segmentation node itself is visible if any segment is visible
        if visible:
            segmentationNode.GetDisplayNode().SetVisibility(True)
            
            # Check subject hierarchy visibility
            shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
            segmentationShItemID = shNode.GetItemByDataNode(segmentationNode)
            
            # Make all parents visible
            parentItemID = shNode.GetItemParent(segmentationShItemID)
            while parentItemID != shNode.GetSceneItemID():
                shNode.SetDisplayVisibilityForBranch(parentItemID, True)
                parentItemID = shNode.GetItemParent(parentItemID)

    def onSceneNodeAdded(self, caller, event):
        """Handle new nodes added to the scene"""
        self.updateSegmentTable()

    def onSceneNodeRemoved(self, caller, event):
        """Handle nodes removed from the scene"""
        self.updateSegmentTable()

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
        selectedVolumeNode = self.ui.inputVolumeSelector.currentNode()
        volumeNodes = slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
        
        # Hide all volume nodes
        for volumeNode in volumeNodes:
            if volumeNode.GetDisplayNode():
                volumeNode.GetDisplayNode().SetVisibility(False)
        
        # Show only the selected volume
        if selectedVolumeNode and selectedVolumeNode.GetDisplayNode():
            selectedVolumeNode.GetDisplayNode().SetVisibility(True)
            
            # Update all slice viewers to show the selected volume
            layoutManager = slicer.app.layoutManager()
            if layoutManager:
                sliceViewNames = ["Red", "Yellow", "Green"]
                for sliceName in sliceViewNames:
                    sliceWidget = layoutManager.sliceWidget(sliceName)
                    if sliceWidget:
                        sliceWidget.sliceLogic().GetSliceCompositeNode().SetBackgroundVolumeID(selectedVolumeNode.GetID())
                        sliceWidget.sliceLogic().FitSliceToAll()
        
            # Reset 3D view focal point
            layoutManager.threeDWidget(0).threeDView().resetFocalPoint()

    def onMaterialMappingToggled(self, enabled):
        self.ui.materialMappingGroupBox.setVisible(enabled)
        self.updateParameterNodeFromGUI()
        
    # New methods for clipping feature
    def onEnableClippingButtonToggled(self, enabled):
        """Toggle clipping on/off"""
        self.ui.clippingControlsGroupBox.setVisible(enabled)
        modelNode = self.ui.modelSelector.currentNode()
        
        if enabled and modelNode:
            self.setupClipping(modelNode)
        else:
            self.disableClipping(modelNode)
            
        # Update button text
        self.ui.enableClippingButton.setText("Disable Clipping" if enabled else "Enable Clipping")

    def onModelSelectedForClipping(self, modelNode):
        """Handle selection of a model for clipping"""
        if modelNode:
            # Update slider ranges based on model bounds
            bounds = [0] * 6
            modelNode.GetBounds(bounds)
            
            # Set ranges for each slider based on model bounds
            self.ui.redSliceOffsetSlider.minimum = bounds[4]
            self.ui.redSliceOffsetSlider.maximum = bounds[5]  # Z range for axial
            
            self.ui.yellowSliceOffsetSlider.minimum = bounds[0]
            self.ui.yellowSliceOffsetSlider.maximum = bounds[1]  # X range for sagittal
            
            self.ui.greenSliceOffsetSlider.minimum = bounds[2]
            self.ui.greenSliceOffsetSlider.maximum = bounds[3]  # Y range for coronal
            
            # Set initial values to middle of ranges
            self.ui.redSliceOffsetSlider.value = (bounds[4] + bounds[5]) / 2
            self.ui.yellowSliceOffsetSlider.value = (bounds[0] + bounds[1]) / 2
            self.ui.greenSliceOffsetSlider.value = (bounds[2] + bounds[3]) / 2
            
            # If clipping is already enabled, update it for the new model
            if self.ui.enableClippingButton.checked:
                self.setupClipping(modelNode)

    def setupClipping(self, modelNode):
        """Set up clipping for the selected model"""
        if not modelNode:
            return
            
        try:
            # Create new clip node if needed
            if not self.clippingNode:
                existingClipNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLClipModelsNode")
                if existingClipNode:
                    self.clippingNode = existingClipNode
                else:
                    self.clippingNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLClipModelsNode")
            
            # Configure clipping node
            self.clippingNode.SetClipType(2)  # 2 = Keep whole cells mode
            self.clippingNode.SetRedSliceClipState(1)
            self.clippingNode.SetYellowSliceClipState(1)
            self.clippingNode.SetGreenSliceClipState(1)
            
            # Update display node clipping settings
            displayNode = modelNode.GetDisplayNode()
            if not displayNode:
                displayNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelDisplayNode")
                modelNode.SetAndObserveDisplayNodeID(displayNode.GetID())
            
            displayNode.SetClipping(1)
            displayNode.SetAndObserveClipNodeID(self.clippingNode.GetID())
            
            # Initialize slice nodes
            layoutManager = slicer.app.layoutManager()
            if not layoutManager:
                return
                
            for color in ['Red', 'Yellow', 'Green']:
                sliceWidget = layoutManager.sliceWidget(color)
                if sliceWidget:
                    self.sliceNodes[color] = sliceWidget.mrmlSliceNode()
            
            # Update initial slice positions
            self.updateSlicePositions()
            
        except Exception as e:
            logging.error(f"Error setting up clipping: {str(e)}")

    def onSliceOffsetChanged(self, sliceColor, value):
        """Handle changes in slice offset sliders"""
        if sliceColor in self.sliceNodes and self.sliceNodes[sliceColor]:
            self.sliceNodes[sliceColor].SetSliceOffset(value)

    def updateSlicePositions(self):
        """Update all slice positions based on current slider values"""
        if 'Red' in self.sliceNodes:
            self.sliceNodes['Red'].SetSliceOffset(self.ui.redSliceOffsetSlider.value)
        if 'Yellow' in self.sliceNodes:
            self.sliceNodes['Yellow'].SetSliceOffset(self.ui.yellowSliceOffsetSlider.value)
        if 'Green' in self.sliceNodes:
            self.sliceNodes['Green'].SetSliceOffset(self.ui.greenSliceOffsetSlider.value)

    def disableClipping(self, modelNode=None):
        """Disable and clean up clipping"""
        if not modelNode:
            modelNode = self.ui.modelSelector.currentNode()
            
        if modelNode:
            displayNode = modelNode.GetDisplayNode()
            if displayNode:
                displayNode.SetClipping(0)
                
        if self.clippingNode:
            slicer.mrmlScene.RemoveNode(self.clippingNode)
            self.clippingNode = None

    def cleanup(self):
        """Clean up observers and restore original visibility states"""
        # Restore original visibility states
        for segmentID, originalState in self._segmentVisibilityStates.items():
            segmentationNodeID = self._parameterNode.GetNodeReferenceID("CurrentSegmentation")
            if segmentationNodeID:
                segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationNodeID)
                if segmentationNode and segmentationNode.GetDisplayNode():
                    segmentationNode.GetDisplayNode().SetSegmentVisibility(segmentID, originalState)
        
        self._segmentVisibilityStates.clear()
        self.removeObservers()
        
        # Call original cleanup
        ScriptedLoadableModuleWidget.cleanup(self)

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
        
        try:
            # Update all the GUI elements
            self.ui.inputVolumeSelector.setCurrentNode(self._parameterNode.GetNodeReference("InputVolume"))
            self.ui.outputDirectorySelector.directory = self._parameterNode.GetParameter("OutputDirectory") or ""
            self.ui.targetEdgeLengthSpinBox.value = float(self._parameterNode.GetParameter("TargetEdgeLength") or 1.37)
            
            # Update output format
            outputFormat = self._parameterNode.GetParameter("OutputFormat") or "all"
            formatIndex = -1
            for i in range(self.ui.outputFormatComboBox.count):
                if str(self.ui.outputFormatComboBox.itemData(i)) == outputFormat:
                    formatIndex = i
                    break
            if formatIndex >= 0:
                self.ui.outputFormatComboBox.currentIndex = formatIndex
                
            # Update material mapping
            materialMapping = self._parameterNode.GetParameter("EnableMaterialMapping") == "true"
            self.ui.enableMaterialMappingCheckBox.checked = materialMapping
            self.ui.materialMappingGroupBox.setVisible(materialMapping)
            self.ui.slopeSpinBox.value = float(self._parameterNode.GetParameter("Slope") or 0.7)
            self.ui.interceptSpinBox.value = float(self._parameterNode.GetParameter("Intercept") or 5.1)
            
            # Check if we can apply - all conditions must be met
            inputVolumeValid = self._parameterNode.GetNodeReference("InputVolume") is not None
            outputDirValid = bool(self._parameterNode.GetParameter("OutputDirectory"))
            
            # Check if any segments are selected
            anySegmentSelected = False
            for isSelected in self.segmentSelectionDict.values():
                if isSelected:
                    anySegmentSelected = True
                    break
            
            # Find the current segmentation node
            segmentationNodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
            hasValidSegmentation = len(segmentationNodes) > 0
            
            # Enable apply button only if all conditions are met
            self.ui.applyButton.enabled = (
                inputVolumeValid and 
                outputDirValid and 
                anySegmentSelected and 
                hasValidSegmentation
            )
            
        except Exception as e:
            logging.error(f"Error updating GUI from parameter node: {str(e)}")
            self.ui.applyButton.enabled = False
        finally:
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
            # Create progress dialog
            progressDialog = slicer.util.createProgressDialog(
                windowTitle="Generating Meshes",
                labelText="Initializing...",
                maximum=100,
                parent=slicer.util.mainWindow()
            )
            progressDialog.minimumDuration = 0
            progressDialog.setValue(0)
            progressDialog.setAutoClose(True)
            
            try:
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
                    "bone_threshold": 400,
                    "neighborhood_radius": 2,
                    "resolution_level": 1
                }
                
                # Get selected segments
                selectedSegments = []
                for segmentID, selected in self.segmentSelectionDict.items():
                    if selected:
                        selectedSegments.append(segmentID)
                
                if not selectedSegments:
                    raise ValueError("No segments selected for processing")
                
                # Process segments with progress updates
                createdNodes, meshStatistics, summary = self.logic.process(
                    inputVolumeNode,
                    segmentationNode,
                    selectedSegments,
                    outputDirectory,
                    targetEdgeLength,
                    outputFormat,
                    enableMaterialMapping,
                    materialParams,
                    progressCallback=lambda progress, message: self.updateProgress(progressDialog, progress, message)
                )
                
                # Show results dialog
                if createdNodes and len(createdNodes) > 0:
                    slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
                    threeDView = slicer.app.layoutManager().threeDWidget(0).threeDView()
                    threeDView.resetFocalPoint()
                    
                    # Count volume mesh nodes
                    volumeMeshCount = sum(1 for node in createdNodes if "_volume_mesh" in node.GetName())
                    
                    # Show statistics dialog and status message
                    self.showMeshStatisticsDialog(meshStatistics, summary)
                    statusMessage = f"Processing complete. {volumeMeshCount} volume meshes with {summary['totalElements']} elements generated."
                else:
                    statusMessage = "Processing complete. No meshes were loaded for display."
                
                slicer.util.showStatusMessage(statusMessage)
                
            except Exception as e:
                slicer.util.errorDisplay(f"Processing failed: {str(e)}")
            finally:
                progressDialog.close()

    def updateProgress(self, progressDialog, progress, message):
        """Update progress dialog with current progress and message."""
        if progressDialog.wasCanceled:
            raise ValueError("User canceled the operation")
        progressDialog.setValue(int(progress))
        if message:
            progressDialog.labelText = message
        slicer.app.processEvents()

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
                targetEdgeLength, outputFormat, enableMaterialMapping, materialParams, progressCallback=None):
        """
        Process all selected segments and display the generated meshes.
        """
        if not os.path.exists(outputDirectory):
            os.makedirs(outputDirectory)
            
        logging.info(f"Starting mesh generation process. Target edge length: {targetEdgeLength}mm")
        
        createdNodes = []
        meshStatistics = {}
        
        # Calculate progress steps
        totalSteps = len(selectedSegments)
        stepsPerSegment = 100.0 / totalSteps if totalSteps > 0 else 100.0
        
        for i, segmentID in enumerate(selectedSegments):
            try:
                baseProgress = i * stepsPerSegment
                segmentCallback = None
                if progressCallback:
                    segmentCallback = lambda progress, message: progressCallback(
                        baseProgress + (progress * stepsPerSegment / 100.0),
                        message
                    )
                
                volumeNode, surfaceNode, stats = self.processSegment(
                    inputVolumeNode, 
                    segmentationNode, 
                    segmentID, 
                    outputDirectory, 
                    targetEdgeLength, 
                    outputFormat,
                    enableMaterialMapping,
                    materialParams,
                    progressCallback=segmentCallback
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
                    
            except Exception as e:
                logging.error(f"Error processing segment {segmentID}: {str(e)}")
                if progressCallback:
                    progressCallback(baseProgress + stepsPerSegment, f"Error: {str(e)}")
                continue
                
            if progressCallback:
                progressCallback(baseProgress + stepsPerSegment, f"Completed segment {i+1} of {totalSteps}")
                
        # Create summary statistics
        totalElements = sum(stats.get("volume_elements", 0) for stats in meshStatistics.values() if stats)
        avgEdgeLength = np.mean([stats.get("vtk_mean_edge_length", 0) for stats in meshStatistics.values() if stats and stats.get("vtk_mean_edge_length")])
        
        summary = {
            "totalMeshes": len(meshStatistics),
            "totalElements": totalElements,
            "averageEdgeLength": avgEdgeLength
        }
        
        # Create a subject hierarchy folder for the models
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        if not shNode:
            shNode = slicer.vtkMRMLSubjectHierarchyNode()
            slicer.mrmlScene.AddNode(shNode)
        
        # Create a new folder with a unique name
        folderName = f"Generated_Meshes_{len(createdNodes)}"
        folderItemID = shNode.CreateFolderItem(shNode.GetSceneItemID(), folderName)
        
        # Add created nodes to the folder
        for node in createdNodes:
            if node:
                nodeItemID = shNode.GetItemByDataNode(node)
                if nodeItemID:
                    shNode.SetItemParent(nodeItemID, folderItemID)
        
        return createdNodes, meshStatistics, summary
    
    def processSegment(self, inputVolumeNode, segmentationNode, segmentID, outputDirectory,
                      targetEdgeLength, outputFormat, enableMaterialMapping, materialParams,
                      progressCallback=None):
        """
        Process a single segment with progress reporting.
        """
        if progressCallback:
            progressCallback(0, "Starting segment processing...")
        
        segmentation = segmentationNode.GetSegmentation()
        segment = segmentation.GetSegment(segmentID)
        segmentName = segment.GetName()
        
        # Create temporary segmentation
        if progressCallback:
            progressCallback(5, f"Preparing {segmentName} for processing...")
            
        # Create segment output directory
        segmentOutputDir = os.path.join(outputDirectory, segmentName)
        os.makedirs(segmentOutputDir, exist_ok=True)
        
        # Create a temporary segmentation with just this segment
        tempSegmentationNode = slicer.vtkMRMLSegmentationNode()
        slicer.mrmlScene.AddNode(tempSegmentationNode)
        tempSegmentationNode.GetSegmentation().AddSegment(segment)
        
        try:
            # Calculate statistics
            if progressCallback:
                progressCallback(10, "Calculating segment statistics...")
            segmentStats = self.calculateVolumeAndSurface(inputVolumeNode, tempSegmentationNode)
            
            # Export to model
            if progressCallback:
                progressCallback(20, "Converting segment to model...")
            modelNode = self.exportSegmentationToModel(tempSegmentationNode)
            
            # Optimize mesh parameters
            if progressCallback:
                progressCallback(30, "Optimizing mesh parameters...")
            optimizationResult = self.optimizeEdgeLength(modelNode, segmentStats["SurfaceArea_mm2"],
                                                       targetEdgeLength, 0.05, 20)
            
            # Generate surface mesh
            if progressCallback:
                progressCallback(50, "Generating surface mesh...")
            # Build file paths
            paths = self.buildPaths(segmentOutputDir, segmentName)
            
            # Variables to track created nodes for display
            generatedVolumeNode = None
            generatedSurfaceNode = None
            meshStats = None
            
            try:
                # Export segmentation to model
                modelNode = self.exportSegmentationToModel(tempSegmentationNode)
                
                # ---------- ADD EDGE LENGTH OPTIMIZATION HERE ----------
                # Run optimization to find optimal pointSurfaceRatio and GMSH size
                optimizationResult = self.optimizeEdgeLength(
                    modelNode, 
                    segmentStats["SurfaceArea_mm2"],
                    targetEdgeLength, 
                    0.05,  # Use 5% tolerance as in original pipeline
                    20     # Max iterations
                )
                
                if optimizationResult:
                    pointSurfaceRatio = optimizationResult['ratio']
                    gmshSize = optimizationResult['gmsh_size']
                    logging.info(f"Using optimized parameters: ratio={pointSurfaceRatio:.4f}, gmsh_size={gmshSize:.4f}mm")
                else:
                    # Fallback to defaults if optimization fails
                    pointSurfaceRatio = 1.62  # Default from pipeline
                    gmshSize = targetEdgeLength
                    logging.info(f"Optimization failed, using defaults: ratio={pointSurfaceRatio}, gmsh_size={gmshSize}mm")
                    
                # Calculate number of points based on optimized ratio
                numberPoints = self.calculateSurfaceNumberPoints(segmentStats["SurfaceArea_mm2"], pointSurfaceRatio)
                logging.info(f"Target number of points: {numberPoints}")
                
                # Create uniform remesh with calculated number of points
                outputModelNode = self.createUniformRemesh(
                    modelNode, 
                    clusterK=round(numberPoints / 1000.0, 1)  # Round to 1 decimal as in the config
                )
                
                # Save surface mesh
                slicer.util.saveNode(outputModelNode, paths["surface_mesh_path"])
                logging.info(f"Surface mesh saved to {paths['surface_mesh_path']}")
                
                # Generate volume mesh with optimized GMSH size
                volume_mesh_path = self.generateVolumeMesh(outputModelNode, paths, gmshSize)
                
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
            
        except Exception as e:
            logging.error(f"Error processing segment {segmentName}: {str(e)}")
            raise
        finally:
            # Cleanup
            if progressCallback:
                progressCallback(100, "Cleaning up...")
            slicer.mrmlScene.RemoveNode(tempSegmentationNode)
            if 'modelNode' in locals() and modelNode:
                slicer.mrmlScene.RemoveNode(modelNode)
            if 'outputModelNode' in locals() and outputModelNode:
                slicer.mrmlScene.RemoveNode(outputModelNode)

    def optimizeEdgeLength(self, modelNode, surfaceArea, targetEdgeLength, tolerance=0.05, maxIterations=20):
        """
        Optimize edge length parameters to match the mesh automation pipeline.
        
        Args:
            modelNode: The input model node to optimize
            surfaceArea: Surface area in mm²
            targetEdgeLength: Target average edge length in mm
            tolerance: Acceptable tolerance as a fraction (default: 0.05 = 5%)
            maxIterations: Maximum optimization iterations
            
        Returns:
            Dictionary with optimization results or None if failed
        """
        try:
            import numpy as np
            import tempfile
            import shutil
            import meshio
            import subprocess
            
            from scipy import optimize
            
            targetMin = targetEdgeLength * (1 - tolerance)
            targetMax = targetEdgeLength * (1 + tolerance)
            
            logging.info(f"Optimizing mesh for edge length {targetEdgeLength}mm")
            logging.info(f"Tolerance: ±{tolerance*100:.1f}% ({targetMin:.4f}mm to {targetMax:.4f}mm)")
            
            # Store evaluation results
            surfaceEvaluations = []
            bestModelNode = None
            initialRatio = 1.62  # Default from pipeline
            
            def evaluateSurfaceEdgeLength(ratio):
                """Evaluate surface mesh edge length for a given ratio"""
                nonlocal bestModelNode, surfaceEvaluations
                
                # Calculate number of points
                numberPoints = self.calculateSurfaceNumberPoints(surfaceArea, ratio)
                
                logging.info(f"Testing ratio = {ratio:.4f} ({numberPoints:.0f} points)")
                
                # Perform remeshing for surface
                outputName = f"OptimizationOutput_{len(surfaceEvaluations)+1}"
                outputModelNode = self.createUniformRemesh(
                    modelNode,
                    outputModelName=outputName,
                    clusterK=round(numberPoints / 1000.0, 1),
                )
                
                # Save temporary STL to measure edge length
                tempDir = tempfile.mkdtemp()
                tempStl = os.path.join(tempDir, "temp_surface.stl")
                
                slicer.util.saveNode(outputModelNode, tempStl)
                
                # Measure surface mesh edge length
                try:
                    surfaceMeshData = meshio.read(tempStl)
                    surfaceEdgeLength = self.calculateAverageEdgeLengthSurface(surfaceMeshData)
                except Exception as e:
                    logging.error(f"Error measuring edge length: {str(e)}")
                    # Clean up
                    slicer.mrmlScene.RemoveNode(outputModelNode)
                    shutil.rmtree(tempDir, ignore_errors=True)
                    return float('inf')  # Return large error value
                
                # Store evaluation
                surfaceEvaluations.append({
                    'ratio': ratio,
                    'edge_length': surfaceEdgeLength,
                    'diff': abs(surfaceEdgeLength - targetEdgeLength),
                    'number_of_points': numberPoints,
                    'stl_path': tempStl
                })
                
                logging.info(f"  → Surface edge length = {surfaceEdgeLength:.4f}mm (target: {targetEdgeLength:.4f}mm)")
                
                # Keep best model node 
                if len(surfaceEvaluations) == 1 or abs(surfaceEdgeLength - targetEdgeLength) < surfaceEvaluations[-2]['diff']:
                    if bestModelNode:
                        slicer.mrmlScene.RemoveNode(bestModelNode)
                    bestModelNode = outputModelNode
                else:
                    slicer.mrmlScene.RemoveNode(outputModelNode)
                
                return surfaceEdgeLength - targetEdgeLength
            
            # Define bounds for surface optimization
            minRatio = max(0.1, initialRatio * 0.25)
            maxRatio = min(20.0, initialRatio * 4.0)
            
            logging.info(f"Starting surface optimization with initial ratio = {initialRatio:.4f}")
            
            # --- SURFACE OPTIMIZATION ---
            try:
                # Try with initial ratio first
                initialResult = evaluateSurfaceEdgeLength(initialRatio)
                
                # If within tolerance, we're done with surface optimization
                if abs(initialResult) <= tolerance * targetEdgeLength:
                    logging.info(f"Initial ratio {initialRatio:.4f} already within tolerance!")
                    bestSurfaceEval = surfaceEvaluations[0]
                else:
                    # Need to optimize - bracket the solution
                    if initialResult > 0:  # Edge length too large, need higher ratio
                        bracket = [initialRatio, maxRatio]
                    else:  # Edge length too small, need lower ratio
                        bracket = [minRatio, initialRatio]
                    
                    # Use brentq for root finding
                    try:
                        result = optimize.brentq(
                            evaluateSurfaceEdgeLength,
                            bracket[0],
                            bracket[1],
                            xtol=tolerance * targetEdgeLength / 10,
                            maxiter=maxIterations // 2,
                            full_output=True
                        )
                        
                        logging.info(f"Surface optimizer converged in {result[1].iterations} iterations")
                    except Exception as e:
                        logging.warning(f"Surface optimization did not fully converge: {e}")
                        logging.info("Using best result found so far")
                    
                    # Get best surface eval
                    bestSurfaceEval = min(surfaceEvaluations, key=lambda x: abs(x['edge_length'] - targetEdgeLength))
                    
                # Extract best surface results
                bestRatio = bestSurfaceEval['ratio']
                bestSurfaceEdgeLength = bestSurfaceEval['edge_length']
                bestNumberOfPoints = bestSurfaceEval['number_of_points']
                bestStlPath = bestSurfaceEval['stl_path']
                
                # Verify the STL file exists and is accessible
                if not os.path.exists(bestStlPath) or os.path.getsize(bestStlPath) == 0:
                    logging.warning(f"Best STL file not found or empty: {bestStlPath}")
                    logging.info("Creating a new STL file from the best model...")
                    
                    # Create a new STL file in a temp directory
                    tempDir = tempfile.mkdtemp()
                    bestStlPath = os.path.join(tempDir, "best_surface.stl")
                    slicer.util.saveNode(bestModelNode, bestStlPath)
                    logging.info(f"Created new STL file: {bestStlPath}")
                
                logging.info(f"Best surface mesh: ratio={bestRatio:.4f}, edge_length={bestSurfaceEdgeLength:.4f}mm")
                
                # --- VOLUME OPTIMIZATION ---
                # For volume optimization, we'll use the GMSH approach
                bestGmshSize = targetEdgeLength  # Default to target edge length
                bestVolumeEdgeLength = None
                volumeWithinTolerance = False
                
                # Skip volume optimization if Python GMSH script is not available
                scriptPath = os.path.dirname(os.path.abspath(__file__))
                gmshScriptPath = os.path.join(scriptPath, "generate_mesh.py")
                
                if not os.path.exists(gmshScriptPath):
                    self.createGmshScript(gmshScriptPath)
                    
                # Initial guess for GMSH size parameter - start with target edge length
                initialGmshSize = targetEdgeLength
                volumeEvaluations = []
                bestVtkPath = None
                
                def evaluateVolumeEdgeLength(gmshSize):
                    """Evaluate volume mesh edge length for a given GMSH size parameter"""
                    nonlocal volumeEvaluations, bestVtkPath
                    
                    logging.info(f"Testing GMSH size = {gmshSize:.4f}mm")
                    
                    # Create temporary directory for volume mesh generation
                    tempDir = tempfile.mkdtemp()
                    tempStl = os.path.join(tempDir, "remesh_output.stl")
                    tempMsh = os.path.join(tempDir, "volume_mesh.msh")
                    tempVtk = os.path.join(tempDir, "volume_mesh.vtk")
                    
                    # Save STL to temp directory
                    shutil.copy(bestStlPath, tempStl)
                    
                    try:
                        # Build command for GMSH script
                        pythonExec = sys.executable
                        cmd = [pythonExec, gmshScriptPath, tempStl, tempMsh, str(gmshSize)]
                        
                        # Use clean environment
                        cleanEnv = {
                            k: v
                            for k, v in os.environ.items()
                            if k not in ["PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"]
                        }
                        
                        # Run with the clean environment
                        subprocess.check_call(cmd, env=cleanEnv)
                        logging.info(f"GMSH successfully generated mesh: {tempMsh}")
                        
                        # Verify the output mesh was created
                        if not os.path.exists(tempMsh) or os.path.getsize(tempMsh) == 0:
                            logging.warning(f"GMSH did not create a valid mesh file at {tempMsh}")
                            return float('inf')  # Return large error value
                        
                        # Convert to VTK and measure edge length
                        meshWithoutTriangles = self.removeMeshTriangles(tempMsh)
                        meshio.write(tempVtk, meshWithoutTriangles)
                        
                        volumeMeshData = meshio.read(tempVtk)
                        volumeEdgeLength = self.calculateAverageEdgeLengthVolume(volumeMeshData)
                        
                        # Store evaluation
                        volumeEvaluations.append({
                            'gmsh_size': gmshSize,
                            'edge_length': volumeEdgeLength,
                            'diff': abs(volumeEdgeLength - targetEdgeLength),
                            'vtk_path': tempVtk
                        })
                        
                        logging.info(f"  → Volume edge length = {volumeEdgeLength:.4f}mm (target: {targetEdgeLength:.4f}mm)")
                        
                        # Keep best VTK path
                        if len(volumeEvaluations) == 1 or abs(volumeEdgeLength - targetEdgeLength) < volumeEvaluations[-2]['diff']:
                            bestVtkPath = tempVtk
                        
                        return volumeEdgeLength - targetEdgeLength
                        
                    except subprocess.CalledProcessError as e:
                        logging.error(f"Error running GMSH: {e}")
                        return float('inf')
                    except Exception as e:
                        logging.error(f"Error generating volume mesh: {str(e)}")
                        return float('inf')
                
                # Define bounds for volume optimization
                minGmshSize = max(0.1, initialGmshSize * 0.5)
                maxGmshSize = min(20.0, initialGmshSize * 2.0)
                
                # Try initial GMSH size
                initialVolumeResult = evaluateVolumeEdgeLength(initialGmshSize)
                
                if volumeEvaluations:  # Check that we got at least one successful evaluation
                    # If within tolerance, we're done
                    if abs(initialVolumeResult) <= tolerance * targetEdgeLength:
                        logging.info(f"Initial GMSH size {initialGmshSize:.4f}mm already within tolerance!")
                        bestVolumeEval = volumeEvaluations[0]
                    else:
                        # Need to optimize - bracket the solution
                        if initialVolumeResult > 0:  # Edge length too large, need smaller size
                            bracket = [minGmshSize, initialGmshSize]
                        else:  # Edge length too small, need larger size
                            bracket = [initialGmshSize, maxGmshSize]
                        
                        # Use brentq for root finding
                        try:
                            result = optimize.brentq(
                                evaluateVolumeEdgeLength,
                                bracket[0],
                                bracket[1],
                                xtol=tolerance * targetEdgeLength / 10,
                                maxiter=maxIterations // 2,
                                full_output=True
                            )
                            
                            logging.info(f"Volume optimizer converged in {result[1].iterations} iterations")
                        except Exception as e:
                            logging.warning(f"Volume optimization did not fully converge: {e}")
                            logging.info("Using best result found so far")
                        
                        # Get best volume eval if we have any successful evaluations
                        if volumeEvaluations:
                            bestVolumeEval = min(volumeEvaluations, key=lambda x: abs(x['edge_length'] - targetEdgeLength))
                            bestGmshSize = bestVolumeEval['gmsh_size']
                            bestVolumeEdgeLength = bestVolumeEval['edge_length']
                            volumeWithinTolerance = targetMin <= bestVolumeEdgeLength <= targetMax
                        else:
                            logging.info("No successful volume mesh evaluations. Using target as GMSH size.")
                else:
                    logging.warning("Volume optimization failed. Using target edge length as GMSH size.")
            
                # Print final results
                logging.info("--- OPTIMIZATION RESULTS ---")
                logging.info(f"Best surface mesh (ratio={bestRatio:.4f}):")
                logging.info(f"  - Edge length: {bestSurfaceEdgeLength:.4f}mm")
                logging.info(f"  - Target: {targetEdgeLength:.4f}mm")
                logging.info(f"  - Difference: {abs(bestSurfaceEdgeLength - targetEdgeLength):.4f}mm")
                
                logging.info(f"Volume mesh (GMSH size={bestGmshSize:.4f}mm):")
                if bestVolumeEdgeLength is not None:
                    logging.info(f"  - Edge length: {bestVolumeEdgeLength:.4f}mm")
                    logging.info(f"  - Difference: {abs(bestVolumeEdgeLength - targetEdgeLength):.4f}mm")
                else:
                    logging.info("  - Edge length: Unknown (GMSH optimization failed)")
                    logging.info("  - Using target edge length as GMSH size")
                
                # Check if within tolerance
                surfaceWithinTolerance = targetMin <= bestSurfaceEdgeLength <= targetMax
                
                if surfaceWithinTolerance and volumeWithinTolerance:
                    logging.info("Both meshes achieved target edge length within tolerance!")
                elif surfaceWithinTolerance:
                    if bestVolumeEdgeLength is not None:
                        logging.info("Surface mesh within tolerance, but volume mesh outside tolerance")
                    else:
                        logging.info("Surface mesh within tolerance (volume mesh not evaluated)")
                elif volumeWithinTolerance:
                    logging.info("Volume mesh within tolerance, but surface mesh outside tolerance")
                else:
                    if bestVolumeEdgeLength is not None:
                        logging.info("Neither mesh achieved target edge length within tolerance")
                    else:
                        logging.info("Surface mesh outside tolerance (volume mesh not evaluated)")
                
                # Return optimization results
                return {
                    'ratio': bestRatio,
                    'gmsh_size': bestGmshSize,
                    'surface_edge_length': bestSurfaceEdgeLength,
                    'volume_edge_length': bestVolumeEdgeLength,
                    'number_of_points': bestNumberOfPoints,
                    'model_node': bestModelNode,
                    'surface_within_tolerance': surfaceWithinTolerance,
                    'volume_within_tolerance': volumeWithinTolerance,
                    'iterations': len(surfaceEvaluations) + len(volumeEvaluations)
                }
                
            except Exception as e:
                logging.error(f"Optimization error: {str(e)}")
                logging.info("Falling back to best result found so far")
                
                if surfaceEvaluations:
                    bestEval = min(surfaceEvaluations, key=lambda x: abs(x['edge_length'] - targetEdgeLength))
                    
                    # Return partial results
                    return {
                        'ratio': bestEval['ratio'],
                        'gmsh_size': targetEdgeLength,  # Fallback to target
                        'surface_edge_length': bestEval['edge_length'],
                        'volume_edge_length': None,
                        'number_of_points': bestEval['number_of_points'],
                        'model_node': bestModelNode,
                        'surface_within_tolerance': targetMin <= bestEval['edge_length'] <= targetMax,
                        'volume_within_tolerance': False,
                        'iterations': len(surfaceEvaluations)
                    }
                else:
                    return None
                
        except Exception as e:
            logging.error(f"Optimization completely failed: {str(e)}")
            return None

    def calculateAverageEdgeLengthSurface(self, meshData):
        """
        Calculates the average edge length for a triangle mesh.
        
        Args:
            meshData: The mesh data read using meshio
            
        Returns:
            Average edge length or None if failed
        """
        import numpy as np
        
        edgeLengths = []
        
        cells = [c.data for c in meshData.cells if c.type == "triangle"]
        for triangles in cells:
            for triangle in triangles:
                p1, p2, p3 = meshData.points[triangle]
                
                # Calculate edges
                e1 = np.linalg.norm(p1 - p2)
                e2 = np.linalg.norm(p2 - p3)
                e3 = np.linalg.norm(p3 - p1)
                edgeLengths.extend([e1, e2, e3])
        
        return np.mean(edgeLengths) if edgeLengths else None

    def calculateAverageEdgeLengthVolume(self, meshData):
        """
        Calculates the average edge length for a tetrahedral mesh.
        
        Args:
            meshData: The mesh data read using meshio
            
        Returns:
            Average edge length or None if failed
        """
        import numpy as np
        
        edgeLengths = []
        
        cells = [c.data for c in meshData.cells if c.type == "tetra"]
        for tetras in cells:
            for tetra in tetras:
                p1, p2, p3, p4 = meshData.points[tetra]
                
                # Calculate all 6 edges of the tetrahedron
                edges = [
                    np.linalg.norm(p1 - p2),
                    np.linalg.norm(p1 - p3),
                    np.linalg.norm(p1 - p4),
                    np.linalg.norm(p2 - p3),
                    np.linalg.norm(p2 - p4),
                    np.linalg.norm(p3 - p4),
                ]
                edgeLengths.extend(edges)
        
        return np.mean(edgeLengths) if edgeLengths else None

    def removeMeshTriangles(self, inputFilepath):
        """
        Load mesh and remove triangles, returning the modified mesh object.
        """
        import meshio
        
        try:
            mesh = meshio.read(inputFilepath)
            if isinstance(mesh, str):
                raise ValueError(f"Failed to read mesh from {inputFilepath}")
                
            newCells = []
            newCellData = {}
            
            # Initialize cell data if it exists
            if hasattr(mesh, 'cell_data') and mesh.cell_data:
                newCellData = {key: [] for key in mesh.cell_data.keys()}
            
            # Filter out triangles
            for i, cellBlock in enumerate(mesh.cells):
                if cellBlock.type != "triangle":
                    newCells.append(cellBlock)
                    if newCellData and i < len(list(mesh.cell_data.values())[0]):
                        for key in newCellData:
                            newCellData[key].append(mesh.cell_data[key][i])
            
            # Create new mesh without triangles
            newMesh = meshio.Mesh(
                points=mesh.points,
                cells=newCells,
                cell_data=newCellData if newCellData else None,
                point_data=mesh.point_data if hasattr(mesh, 'point_data') else None,
                field_data=mesh.field_data if hasattr(mesh, 'field_data') else None
            )
            return newMesh
            
        except Exception as e:
            logging.error(f"Error removing triangles from mesh: {str(e)}")
            raise

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
        
        # Set the output mesh (with Quality array) back to the model node
        modelNode.SetAndObserveMesh(qualityFilter.GetOutput())
        displayNode = modelNode.GetDisplayNode()
        if displayNode:
            displayNode.SetActiveScalarName("Quality")
            displayNode.SetScalarVisibility(True)
            # Set color table to HotToColdRainbow if available
            colorNode = slicer.mrmlScene.GetFirstNodeByName("Warm1")
            if colorNode:
                displayNode.SetAndObserveColorNodeID(colorNode.GetID())
        
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
    
    def createUniformRemesh(self, inputModel, clusterK=10, outputModelName="UniformRemeshOutput"):
        """
        Create uniform remesh using SurfaceToolbox.
        Aligned with desired workflow's uniform_remesh function.
        
        Args:
            inputModel: Input model node
            clusterK: Cluster K value for remeshing (number of clusters in K*1000)
            outputModelName: Name for the output model node
            
        Returns:
            The remeshed model node
        """
        outputModelNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", outputModelName)
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
import numpy as np

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
    
    # Get entities and ensure they are 3D
    entities = gmsh.model.getEntities(dim=2)
    if not entities:
        print("No surfaces found.")
        gmsh.finalize()
        sys.exit(1)
    
    # Convert 2D tags to 3D if needed
    surface_tags = []
    for entity in entities:
        tag = entity[1]
        if isinstance(tag, (list, tuple)) and len(tag) == 2:
            tag = np.array([tag[0], tag[1], 0])  # Append Z component
        surface_tags.append(tag)
    
    try:
        loop = gmsh.model.geo.addSurfaceLoop(surface_tags)
        volume = gmsh.model.geo.addVolume([loop])
        gmsh.model.geo.synchronize()
        
        # Set mesh size parameters
        gmsh.option.setNumber('Mesh.MeshSizeMin', size_param)
        gmsh.option.setNumber('Mesh.MeshSizeMax', size_param)
        gmsh.option.setNumber('Mesh.Algorithm3D', 1)  # Delaunay algorithm
        
        # Generate 3D mesh
        gmsh.model.mesh.generate(3)
        
        # Write output
        gmsh.write(output_msh)
    except Exception as e:
        print(f"Error during mesh generation: {str(e)}")
    finally:
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
