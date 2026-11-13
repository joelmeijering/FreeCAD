# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
from FreeCAD import Vector, Base, newDocument, closeDocument
import Part

import math
import os

if "BUILD_SKETCHER" in FreeCAD.__cmake__:
    import Sketcher

import unittest


class RegressionTests(unittest.TestCase):

    # pylint: disable=attribute-defined-outside-init

    def setUp(self):
        """Create a document for each test in the test suite"""
        self.Doc = newDocument("PartRegressionTest." + self._testMethodName)
        self.KeepTestDoc = False

    def test_refine_cylinder_face_repair_failure(self):
        """A failed cylinder merge must not abort refinement of other faces."""
        # Two valid adjacent cylindrical faces whose proposed union makes
        # OCCT 7.8's ShapeFix_Face::FixOrientation throw "Bnd_Box is void".
        shell = Part.Shape()
        shell.read(os.path.join(os.path.dirname(__file__), "refine_cylinder_void_box.brep"))
        self.assertTrue(shell.isValid())
        self.assertEqual(len(shell.Faces), 2)

        boxes = Part.makeBox(1, 1, 1, Vector(100, 0, 0)).fuse(
            Part.makeBox(1, 1, 1, Vector(101, 0, 0))
        )
        self.assertEqual(len(boxes.Faces), 10)
        original = Part.makeCompound([shell, boxes])
        refined = original.removeSplitter()

        self.assertTrue(original.isValid())
        self.assertTrue(refined.isValid())
        self.assertEqual(len(refined.Solids), 1)
        self.assertEqual(len(refined.Solids[0].Faces), 6)
        self.assertAlmostEqual(refined.Solids[0].Volume, 2.0, places=7)
        self.assertAlmostEqual(refined.Area, original.Area, places=7)
        # The troublesome faces are retained, not removed from the shell.
        shells = [child for child in refined.childShapes() if child.ShapeType == "Shell"]
        self.assertEqual(len(shells), 1)
        self.assertAlmostEqual(shells[0].Area, shell.Area, places=7)

    def test_issue_4456(self):
        """
        0004456: Regression : Part.Plane.Intersect does not accept plane as argument
        """
        p1 = Part.Plane()
        p2 = Part.Plane(Vector(0, 0, 0), Vector(1, 0, 0))

        result = p1.intersect(p2)
        line = result.pop()
        self.assertEqual(line.Location, Vector(0, 0, 0))
        self.assertEqual(line.Direction, Vector(0, 1, 0))
        # We should now have empty list...
        with self.assertRaises(IndexError):
            result.pop()

    def test_issue_15735(self):
        """
        15735: Point in sketch as loft profile won't work in dev, but works in stable
        The following test is a simplified version of the issue, but the outcome is the same
        """

        if "BUILD_SKETCHER" in FreeCAD.__cmake__:
            # Arrange
            ArcSketch = self.Doc.addObject("Sketcher::SketchObject", "ArcSketch")
            ArcSketch.Placement = Base.Placement(
                Base.Vector(0.000000, 0.000000, 0.000000),
                Base.Rotation(0.500000, 0.500000, 0.500000, 0.500000),
            )
            ArcSketch.MapMode = "Deactivated"

            geoList = []
            geoList.append(
                Part.ArcOfCircle(
                    Part.Circle(
                        Base.Vector(0.000000, 0.000000, 0.000000),
                        Base.Vector(0.000000, 0.000000, 1.000000),
                        10.000000,
                    ),
                    3.141593,
                    6.283185,
                )
            )
            ArcSketch.addGeometry(geoList, False)
            del geoList

            constraintList = []
            ArcSketch.addConstraint(Sketcher.Constraint("Radius", 0, 10.000000))
            constraintList.append(Sketcher.Constraint("Coincident", 0, 3, -1, 1))
            constraintList.append(Sketcher.Constraint("PointOnObject", 0, 2, -1))
            constraintList.append(Sketcher.Constraint("PointOnObject", 0, 1, -1))
            ArcSketch.addConstraint(constraintList)
            del constraintList

            self.Doc.recompute()

            PointSketch = self.Doc.addObject("Sketcher::SketchObject", "PointSketch")
            PointSketch.Placement = Base.Placement(
                Base.Vector(-10.000000, 0.000000, 0.000000),
                Base.Rotation(0.500000, 0.500000, 0.500000, 0.500000),
            )
            PointSketch.MapMode = "Deactivated"

            PointSketch.addGeometry(Part.Point(Base.Vector(0.000000, 0.000000, 0)))

            PointSketch.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))

            self.Doc.recompute()

            Loft = self.Doc.addObject("Part::Loft", "Loft")
            Loft.Sections = [
                ArcSketch,
                PointSketch,
            ]
            Loft.Solid = False
            Loft.Ruled = False
            Loft.Closed = False

            # Act
            self.Doc.recompute()

            # Assert
            self.assertTrue(Loft.isValid())
            self.KeepTestDoc = not Loft.isValid()

    def test_OptimalBox(self):
        box = Part.makeBox(1, 1, 1)
        self.assertTrue(box.optimalBoundingBox(True, False).isValid())

    def test_CircularReference(self):

        cube = self.Doc.addObject("Part::Box", "Cube")
        cube.setExpression("Length", "Width + 10mm")
        with self.assertRaises(RuntimeError) as context:
            cube.setExpression("Width", "Length + 10mm")
        assert "Width reference creates a cyclic dependency." in str(context.exception)

        cube.setExpression(".Placement.Base.x", ".Placement.Base.y + 10mm")
        with self.assertRaises(RuntimeError) as context:
            cube.setExpression(".Placement.Base.y", ".Placement.Base.x + 10mm")
        assert ".Placement.Base.y reference creates a cyclic dependency." in str(context.exception)

        cube.recompute()
        v1 = cube.Placement.Base
        cube.recompute()
        assert cube.Placement.Base.isEqual(v1, 1e-6)

    def test_TangentMode3_Issue24254(self):
        """In FreeCAD 1.1 we changed the behavior of the mmTangentPlane, but added code to handle old files
        that relied upon the original tangent plane behavior. This test ensures that old files open with the expected
        tangent plane setup."""

        location = os.path.dirname(os.path.realpath(__file__))
        FreeCAD.openDocument(os.path.join(location, "TestTangentMode3-0.21.FCStd"), True)

        def check_plane(name, expected_pos, expected_normal, expected_xaxis):
            obj = FreeCAD.ActiveDocument.getObject(name)
            if not obj:
                FreeCAD.Console.PrintError(f"{name}: object not found\n")
                return

            pos = obj.Placement.Base
            rot = obj.Placement.Rotation
            normal = rot.multVec(FreeCAD.Vector(0, 0, 1))
            xaxis = rot.multVec(FreeCAD.Vector(1, 0, 0))

            # position
            self.assertAlmostEqual((pos - expected_pos).Length, 0)

            # normal
            self.assertAlmostEqual(normal.getAngle(expected_normal), 0)

            # tangent x-axis
            self.assertAlmostEqual(xaxis.getAngle(expected_xaxis), 0)

        r = 10.0
        rad30 = math.radians(30)
        cos30 = math.cos(rad30)
        sin30 = math.sin(rad30)

        expected_planes = [
            (
                "Sketch001",
                FreeCAD.Vector(-1 * r, 0, 0),  # position
                FreeCAD.Vector(1, 0, 0),  # normal
                FreeCAD.Vector(0, -1, 0),
            ),  # tangent x-axis
            (
                "Sketch002",
                FreeCAD.Vector(sin30 * r, cos30 * cos30 * (-r), cos30 * sin30 * r),
                FreeCAD.Vector(sin30, cos30 * cos30 * (-1), cos30 * sin30),
                FreeCAD.Vector(0, sin30, cos30),
            ),
            (
                "Sketch003",
                FreeCAD.Vector(cos30 * cos30 * r, sin30 * r, cos30 * sin30 * r),
                FreeCAD.Vector(cos30 * cos30, sin30, cos30 * sin30),
                FreeCAD.Vector(sin30, 0, -cos30),
            ),
        ]

        for name, pos, normal, xaxis in expected_planes:
            check_plane(name, pos, normal, xaxis)

    def test_issue_29733_transformShape_bakes_in_transformation(self):
        """
        29733: transformShape(matrix, True) must bake the transformation into the
        underlying geometry, leaving the shape's Placement untouched. Part Explode
        Compound and many third-party addons depend on this behavior.
        """
        sphere = Part.makeSphere(5.0)
        move = FreeCAD.Placement(FreeCAD.Vector(10, 0, 0), FreeCAD.Rotation()).toMatrix()

        returned = sphere.transformShape(move, True)

        self.assertIs(returned, sphere)
        self.assertAlmostEqual(sphere.Placement.Base.x, 0.0)
        self.assertAlmostEqual(sphere.Placement.Base.y, 0.0)
        self.assertAlmostEqual(sphere.Placement.Base.z, 0.0)
        self.assertAlmostEqual(sphere.CenterOfGravity.x, 10.0, places=5)
        self.assertAlmostEqual(sphere.CenterOfGravity.y, 0.0, places=5)
        self.assertAlmostEqual(sphere.CenterOfGravity.z, 0.0, places=5)

    def test_transformed_returns_independent_baked_in_copy(self):
        """
        transformed() must return a new shape with the transformation baked into
        the geometry, leaving the original shape unchanged.
        """
        sphere = Part.makeSphere(5.0)
        move = FreeCAD.Placement(FreeCAD.Vector(10, 0, 0), FreeCAD.Rotation()).toMatrix()

        moved = sphere.transformed(move, copy=True)

        self.assertIsNot(moved, sphere)
        self.assertAlmostEqual(sphere.CenterOfGravity.x, 0.0, places=5)
        self.assertAlmostEqual(moved.Placement.Base.x, 0.0)
        self.assertAlmostEqual(moved.CenterOfGravity.x, 10.0, places=5)

    def test_issue_15716_AttacherEngine_sync(self):
        """
        15716: AttacherType and AttacherEngine property conflict

        When creating a Part2DObject (like a Sketch), the AttacherEngine property
        should be synchronized with AttacherType. Previously, AttacherType was
        correctly set to "Attacher::AttachEnginePlane" but AttacherEngine stayed
        at the default "Engine 3D" instead of "Engine Plane".
        """
        if "BUILD_SKETCHER" in FreeCAD.__cmake__:
            # Create a new sketch (which inherits from Part2DObject)
            sketch = self.Doc.addObject("Sketcher::SketchObject", "TestSketch")
            self.Doc.recompute()

            # The visible AttacherEngine property should match the actual engine type
            # AttacherType is the internal type name, AttacherEngine is the user-visible enum
            attacher_type = sketch.AttacherType
            attacher_engine = sketch.AttacherEngine

            # For a sketch, AttacherType should be AttachEnginePlane
            self.assertEqual(attacher_type, "Attacher::AttachEnginePlane")

            # AttacherEngine should show "Engine Plane" (not "Engine 3D")
            self.assertEqual(attacher_engine, "Engine Plane")

    def test_getSortedClusters(self):
        "Part.getSortedClusters() may crash with short edges"
        poly = self.Doc.addObject("Part::RegularPolygon", "RegularPolygon")
        poly.Circumradius = 1
        for i in range(3, 1004, 50):
            poly.Polygon = i
            poly.recompute()
            clusters = Part.getSortedClusters(poly.Shape.Edges)

            # should be only one cluster
            self.assertEqual(len(clusters), 1)

            # cluster should contain all edges
            self.assertEqual(len(clusters[0]), i)

    def tearDown(self):
        """Clean up our test, optionally preserving the test document"""
        # This flag allows doing something like this:
        #   self.KeepTestDoc = True
        #   import TestApp
        #   TestApp.Test("TestPartApp.RegressionTests.test_issue_15735")
        # to leave the test document(s) around for further examination in an interactive setting.
        if hasattr(self, "KeepTestDoc") and self.KeepTestDoc:
            return
        closeDocument(self.Doc.Name)
