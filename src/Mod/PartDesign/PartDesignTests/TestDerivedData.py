# SPDX-License-Identifier: LGPL-2.1-or-later

import pathlib
import shutil
import tempfile
import unittest
import zipfile

import FreeCAD as App
import Part
import Sketcher


class _FeatureSliceImpostor:
    """A proxy with a misleading Type string that must remain authored."""

    Type = "FeatureSlice"

    def execute(self, _object):
        return


class TestDerivedData(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._document_uid = None

    def tearDown(self):
        for document in list(App.listDocuments().values()):
            if document.Name.startswith("PartDesignDerivedData"):
                App.closeDocument(document.Name)

        if self._document_uid:
            cache_root = pathlib.Path(App.getUserCachePath()) / "derived"
            for uid_directory in cache_root.glob(f"v*/*/{self._document_uid}"):
                shutil.rmtree(uid_directory, ignore_errors=True)

        self._temporary_directory.cleanup()

    @staticmethod
    def _first_valid_dressup(body, type_name, name, source, size_property, size):
        document = body.Document
        dressup = document.addObject(type_name, name)
        body.addObject(dressup)
        setattr(dressup, size_property, size)
        for index in range(1, len(source.Shape.Edges) + 1):
            dressup.Base = (source, [f"Edge{index}"])
            dressup.touch()
            document.recompute()
            if dressup.isValid() and not dressup.Shape.isNull():
                return dressup
        raise AssertionError(f"No valid edge found for {type_name}")

    @staticmethod
    def _edge_signature(target, name):
        edge = target.getSubObject(name)
        box = edge.BoundBox
        return (
            edge.Length,
            edge.CenterOfMass.x,
            edge.CenterOfMass.y,
            edge.CenterOfMass.z,
            box.XMin,
            box.YMin,
            box.ZMin,
            box.XMax,
            box.YMax,
            box.ZMax,
        )

    @staticmethod
    def _shape_signature(shape):
        box = shape.BoundBox
        return (
            shape.ShapeType,
            len(shape.Solids),
            len(shape.Faces),
            len(shape.Edges),
            len(shape.Vertexes),
            round(shape.Volume, 9),
            round(shape.Area, 9),
            round(shape.Length, 9),
            round(box.XMin, 9),
            round(box.YMin, 9),
            round(box.ZMin, 9),
            round(box.XMax, 9),
            round(box.YMax, 9),
            round(box.ZMax, 9),
        )

    def _assert_valid_chain(self, document):
        box = document.getObject("Pad")
        fillet = document.getObject("Fillet")
        chamfer = document.getObject("Chamfer")
        for feature in (box, fillet, chamfer):
            self.assertTrue(feature.isValid(), feature.State)
            self.assertFalse(feature.Shape.isNull())
            self.assertGreater(feature.Shape.ElementMapSize, 0)
        self.assertFalse(fillet.Base[1][0].startswith("?"))
        self.assertFalse(chamfer.Base[1][0].startswith("?"))
        return box, fillet, chamfer

    def testDatumColdRestoreDefersAttachmentUntilSupportExists(self):
        root = pathlib.Path(self._temporary_directory.name)
        source_file = root / "datum.FCStd"
        moved_file = root / "datum-cold.FCStd"
        document = App.newDocument("PartDesignDerivedDataDatum")
        body = document.addObject("PartDesign::Body", "Body")
        sketch = body.newObject("Sketcher::SketchObject", "Sketch")
        sketch.addGeometry(
            Part.Circle(App.Vector(10, 20, 0), App.Vector(0, 0, 1), 5), False
        )
        source = body.newObject("PartDesign::Pad", "SourcePad")
        source.Profile = sketch
        source.Length = 5
        document.recompute()
        plane = body.newObject("PartDesign::Plane", "Plane")
        plane.AttachmentSupport = (source, ["Vertex1"])
        plane.MapMode = "Translate"
        consumer = body.newObject("Sketcher::SketchObject", "Consumer")
        consumer.AttachmentSupport = (plane, [""])
        consumer.MapMode = "FlatFace"
        consumer.addGeometry(
            Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 2), False
        )
        document.recompute()
        expected_position = plane.Placement.Base
        expected_shape = self._shape_signature(consumer.Shape)
        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        class Observer:
            def __init__(self):
                self.support_notifications = 0
                self.invalid_events = []

            def slotChangedObject(self, obj, prop):
                if obj.Name == "Plane":
                    if prop == "AttachmentSupport":
                        self.support_notifications += 1
                    if not obj.isValid():
                        self.invalid_events.append((prop, obj.State))

        shutil.copy2(source_file, moved_file)
        for path in (source_file, moved_file):
            observer = Observer()
            App.addDocumentObserver(observer)
            try:
                document = App.openDocument(str(path))
            finally:
                App.removeDocumentObserver(observer)
            # The view still receives the support notification, without an
            # early attachment evaluation against a not-yet-rebuilt solid.
            self.assertGreater(observer.support_notifications, 0)
            self.assertFalse(observer.invalid_events, observer.invalid_events)
            self.assertTrue(document.Plane.isValid())
            self.assertTrue(document.Consumer.isValid())
            self.assertLess((document.Plane.Placement.Base - expected_position).Length, 1e-9)
            self.assertEqual(expected_shape, self._shape_signature(document.Consumer.Shape))
            App.closeDocument(document.Name)

        # A genuinely missing subelement must still fail during recompute.
        document = App.openDocument(str(moved_file))
        document.Plane.AttachmentSupport = (document.SourcePad, ["Vertex999"])
        document.recompute()
        self.assertFalse(document.Plane.isValid())

    def testColdReconstructionPreservesDressUpReferences(self):
        root = pathlib.Path(self._temporary_directory.name)
        source_directory = root / "source"
        moved_directory = root / "moved"
        source_directory.mkdir()
        moved_directory.mkdir()
        source_file = source_directory / "model.FCStd"
        moved_file = moved_directory / "model.FCStd"

        document = App.newDocument("PartDesignDerivedDataCreate")
        body = document.addObject("PartDesign::Body", "Body")
        sketch = body.newObject("Sketcher::SketchObject", "Sketch")
        sketch.addGeometry(
            [
                Part.LineSegment(App.Vector(0, 0), App.Vector(20, 0)),
                Part.LineSegment(App.Vector(20, 0), App.Vector(20, 15)),
                Part.LineSegment(App.Vector(20, 15), App.Vector(0, 15)),
                Part.LineSegment(App.Vector(0, 15), App.Vector(0, 0)),
            ],
            False,
        )
        sketch.addConstraint(
            [
                Sketcher.Constraint("Coincident", 0, 2, 1, 1),
                Sketcher.Constraint("Coincident", 1, 2, 2, 1),
                Sketcher.Constraint("Coincident", 2, 2, 3, 1),
                Sketcher.Constraint("Coincident", 3, 2, 0, 1),
            ]
        )
        box = body.newObject("PartDesign::Pad", "Pad")
        box.Profile = sketch
        box.Length = 10
        document.recompute()
        fillet = self._first_valid_dressup(body, "PartDesign::Fillet", "Fillet", box, "Radius", 1)
        chamfer = self._first_valid_dressup(
            body, "PartDesign::Chamfer", "Chamfer", fillet, "Size", 0.5
        )
        document.recompute()

        expected_volumes = tuple(feature.Shape.Volume for feature in (box, fillet, chamfer))
        expected_maps = tuple(dict(feature.Shape.ElementMap) for feature in (box, fillet, chamfer))
        expected_edges = (
            self._edge_signature(box, fillet.Base[1][0]),
            self._edge_signature(fillet, chamfer.Base[1][0]),
        )
        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        with zipfile.ZipFile(source_file) as archive:
            self.assertEqual(archive.namelist(), ["Document.xml"])
            self.assertGreater(archive.read("Document.xml").count(b'Derived="1"'), 3)

        # Same-path open exercises a cache hit.
        document = App.openDocument(str(source_file))
        box, fillet, chamfer = self._assert_valid_chain(document)
        self.assertEqual(
            expected_volumes,
            tuple(feature.Shape.Volume for feature in (box, fillet, chamfer)),
        )
        self.assertEqual(
            expected_maps,
            tuple(dict(feature.Shape.ElementMap) for feature in (box, fillet, chamfer)),
        )
        App.closeDocument(document.Name)

        # A different absolute path deliberately makes this a cold-cache open.
        shutil.copy2(source_file, moved_file)
        document = App.openDocument(str(moved_file))
        box, fillet, chamfer = self._assert_valid_chain(document)
        self.assertEqual(
            expected_volumes,
            tuple(feature.Shape.Volume for feature in (box, fillet, chamfer)),
        )
        self.assertEqual(
            expected_maps,
            tuple(dict(feature.Shape.ElementMap) for feature in (box, fillet, chamfer)),
        )
        self.assertEqual(
            expected_edges,
            (
                self._edge_signature(box, fillet.Base[1][0]),
                self._edge_signature(fillet, chamfer.Base[1][0]),
            ),
        )

        old_final_volume = chamfer.Shape.Volume
        box.Length = 12
        document.recompute()
        self._assert_valid_chain(document)
        self.assertNotEqual(old_final_volume, chamfer.Shape.Volume)

    def testShapeStringColdReconstruction(self):
        from draftmake.make_shapestring import make_shapestring

        root = pathlib.Path(self._temporary_directory.name)
        source_directory = root / "source"
        moved_directory = root / "moved"
        source_directory.mkdir()
        moved_directory.mkdir()
        source_file = source_directory / "shapestring.FCStd"
        moved_file = moved_directory / "shapestring.FCStd"
        font_file = (
            pathlib.Path(App.getResourceDir())
            / "Mod"
            / "TechDraw"
            / "Resources"
            / "fonts"
            / "osifont-lgpl3fe.ttf"
        )

        document = App.newDocument("PartDesignDerivedDataShapeString")
        shape_string = make_shapestring("Derived", str(font_file), Size=12, Tracking=0.5)
        document.recompute()
        self.assertTrue(shape_string.isValid(), shape_string.State)
        self.assertFalse(shape_string.Shape.isNull())
        expected_signature = self._shape_signature(shape_string.Shape)
        expected_map = dict(shape_string.Shape.ElementMap)
        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        with zipfile.ZipFile(source_file) as archive:
            self.assertEqual(archive.namelist(), ["Document.xml"])
            self.assertIn(b'Derived="1"', archive.read("Document.xml"))

        document = App.openDocument(str(source_file))
        shape_string = document.getObject("ShapeString")
        self.assertTrue(shape_string.isValid(), shape_string.State)
        self.assertEqual(expected_signature, self._shape_signature(shape_string.Shape))
        self.assertEqual(expected_map, dict(shape_string.Shape.ElementMap))
        App.closeDocument(document.Name)

        shutil.copy2(source_file, moved_file)
        document = App.openDocument(str(moved_file))
        shape_string = document.getObject("ShapeString")
        self.assertTrue(shape_string.isValid(), shape_string.State)
        self.assertEqual(expected_signature, self._shape_signature(shape_string.Shape))
        self.assertEqual(expected_map, dict(shape_string.Shape.ElementMap))

        # An attached ShapeString receives Placement and Shape notifications
        # during cold restoration.  This must not take the placement-only fast
        # path while its omitted derived Shape is still null.
        shape_string.Shape = Part.Shape()
        shape_string.Proxy.props_changed = ["Placement", "Shape"]
        shape_string.Proxy.execute(shape_string)
        self.assertFalse(shape_string.Shape.isNull())
        self.assertEqual(expected_signature, self._shape_signature(shape_string.Shape))
        self.assertEqual(expected_map, dict(shape_string.Shape.ElementMap))

        shape_string.String = ""
        document.recompute()
        self.assertTrue(shape_string.Shape.isNull())

    def testShapeStringMissingFontFailsColdReconstruction(self):
        from draftmake.make_shapestring import make_shapestring

        root = pathlib.Path(self._temporary_directory.name)
        source_directory = root / "source"
        moved_directory = root / "moved"
        source_directory.mkdir()
        moved_directory.mkdir()
        source_file = source_directory / "shapestring.FCStd"
        moved_file = moved_directory / "shapestring.FCStd"
        installed_font = (
            pathlib.Path(App.getResourceDir())
            / "Mod"
            / "TechDraw"
            / "Resources"
            / "fonts"
            / "osifont-lgpl3fe.ttf"
        )
        temporary_font = root / "font.ttf"
        shutil.copy2(installed_font, temporary_font)

        document = App.newDocument("PartDesignDerivedDataMissingFont")
        shape_string = make_shapestring("Derived", str(temporary_font), Size=12)
        document.recompute()
        self.assertTrue(shape_string.isValid(), shape_string.State)
        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        temporary_font.unlink()
        shutil.copy2(source_file, moved_file)
        document = App.openDocument(str(moved_file))
        shape_string = document.getObject("ShapeString")
        self.assertFalse(shape_string.isValid())
        self.assertTrue(shape_string.Shape.isNull())

    def testSliceApartColdReconstruction(self):
        from BOPTools.SplitFeatures import makeSlice
        from CompoundTools.Explode import explodeCompound

        root = pathlib.Path(self._temporary_directory.name)
        source_directory = root / "source"
        moved_directory = root / "moved"
        source_directory.mkdir()
        moved_directory.mkdir()
        source_file = source_directory / "slice-apart.FCStd"
        moved_file = moved_directory / "slice-apart.FCStd"

        document = App.newDocument("PartDesignDerivedDataSliceApart")
        base = document.addObject("Part::Feature", "Base")
        base.Shape = Part.makeBox(20, 15, 10)
        tool = document.addObject("Part::Feature", "Tool")
        tool.Shape = Part.makePlane(30, 25, App.Vector(-5, -5, 4))
        sliced = makeSlice("Slice")
        sliced.Base = base
        sliced.Tools = [tool]
        sliced.Mode = "Split"
        sliced.Proxy.execute(sliced)
        sliced.purgeTouched()
        group, children = explodeCompound(sliced)
        document.recompute()

        self.assertEqual(2, len(children))
        self.assertTrue(sliced.isValid(), sliced.State)
        self.assertTrue(all(child.isValid() for child in children))
        expected_slice_signature = self._shape_signature(sliced.Shape)
        expected_slice_map = dict(sliced.Shape.ElementMap)
        group_name = group.Name
        child_names = [child.Name for child in children]
        expected_child_signatures = [self._shape_signature(child.Shape) for child in children]
        expected_child_maps = [dict(child.Shape.ElementMap) for child in children]

        consumer = document.addObject("App::FeaturePython", "Consumer")
        consumer.addProperty("App::PropertyLinkSub", "Reference")
        consumer.Reference = (children[0], ["Face1"])
        expected_reference = consumer.Reference[1][0]

        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        with zipfile.ZipFile(source_file) as archive:
            document_xml = archive.read("Document.xml")
            self.assertGreaterEqual(document_xml.count(b'Derived="1"'), len(children) + 1)

        def assert_reconstructed(restored_document):
            restored_slice = restored_document.getObject("Slice")
            restored_children = [restored_document.getObject(name) for name in child_names]
            restored_group = restored_document.getObject(group_name)
            restored_consumer = restored_document.getObject("Consumer")

            self.assertTrue(restored_slice.isValid(), restored_slice.State)
            self.assertTrue(all(child.isValid() for child in restored_children))
            self.assertEqual(
                expected_slice_signature,
                self._shape_signature(restored_slice.Shape),
            )
            self.assertEqual(expected_slice_map, dict(restored_slice.Shape.ElementMap))
            self.assertEqual(
                expected_child_signatures,
                [self._shape_signature(child.Shape) for child in restored_children],
            )
            self.assertEqual(
                expected_child_maps,
                [dict(child.Shape.ElementMap) for child in restored_children],
            )
            self.assertEqual(child_names, [child.Name for child in restored_group.Group])
            self.assertIs(restored_slice, restored_children[0].Base)
            self.assertIs(restored_children[0], restored_consumer.Reference[0])
            self.assertEqual(expected_reference, restored_consumer.Reference[1][0])
            self.assertFalse(
                restored_children[0].getSubObject(restored_consumer.Reference[1][0]).isNull()
            )

        document = App.openDocument(str(source_file))
        assert_reconstructed(document)
        App.closeDocument(document.Name)

        shutil.copy2(source_file, moved_file)
        document = App.openDocument(str(moved_file))
        assert_reconstructed(document)

    def testSliceApartCollisionFiltersColdReconstruction(self):
        from BOPTools.SplitFeatures import makeSlice
        from CompoundTools.CompoundFilter import makeCompoundFilter

        root = pathlib.Path(self._temporary_directory.name)
        source_file = root / "source.FCStd"
        moved_file = root / "moved.FCStd"
        document = App.newDocument("PartDesignDerivedDataCollisionFilters")
        base = document.addObject("Part::Feature", "Base")
        base.Shape = Part.makeBox(20, 15, 10)
        planes = []
        for name, height in (("LowPlane", 4), ("HighPlane", 7)):
            plane = document.addObject("Part::Feature", name)
            plane.Shape = Part.makePlane(30, 25, App.Vector(-5, -5, height))
            planes.append(plane)
        sliced = makeSlice("Slice")
        sliced.Base = base
        sliced.Tools = planes
        sliced.Mode = "Split"

        def select(name, source, stencil, invert=False):
            child = makeCompoundFilter(name)
            child.Base = source
            child.FilterType = "collision-pass"
            child.Stencil = stencil
            child.Invert = invert
            return child

        upper_pair = select("UpperPair", sliced, planes[1])
        middle = select("Middle", upper_pair, planes[0])
        upper = select("Upper", sliced, planes[0], True)
        lower = select("Lower", sliced, planes[1], True)
        # A filter outside a Slice chain must retain the conservative policy.
        retained = select("UnrelatedFilter", base, planes[0])
        document.recompute()
        children = (upper_pair, middle, upper, lower)
        expected = {
            child.Name: self._shape_signature(child.Shape) for child in children
        }
        self.assertEqual(len(upper_pair.Shape.Solids), 2)
        for child, volume in ((middle, 900), (upper, 900), (lower, 1200)):
            self.assertEqual(len(child.Shape.Solids), 1)
            self.assertAlmostEqual(child.Shape.Volume, volume, places=7)
        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        with zipfile.ZipFile(source_file) as archive:
            self.assertGreaterEqual(archive.read("Document.xml").count(b'Derived="1"'), 5)
            for name in expected:
                self.assertFalse(any(entry.startswith(name + ".Shape.")
                                     for entry in archive.namelist()))
            self.assertTrue(any(entry.startswith("UnrelatedFilter.Shape.")
                                for entry in archive.namelist()))

        for path in (source_file, moved_file):
            if path == moved_file:
                shutil.copy2(source_file, moved_file)
            restored = App.openDocument(str(path))
            for name, signature in expected.items():
                child = restored.getObject(name)
                self.assertTrue(child.isValid(), child.State)
                self.assertEqual(signature, self._shape_signature(child.Shape))
            App.closeDocument(restored.Name)

    def testPythonProxyTypeStringDoesNotQualifyDerivedShape(self):
        root = pathlib.Path(self._temporary_directory.name)
        source_file = root / "impostor.FCStd"
        document = App.newDocument("PartDesignDerivedDataImpostor")
        impostor = document.addObject("Part::FeaturePython", "Impostor")
        impostor.Proxy = _FeatureSliceImpostor()
        impostor.Shape = Part.makeBox(1, 2, 3)
        impostor.purgeTouched()
        self._document_uid = document.Uid
        document.saveAs(str(source_file))
        App.closeDocument(document.Name)

        with zipfile.ZipFile(source_file) as archive:
            self.assertNotEqual(["Document.xml"], archive.namelist())
            self.assertNotIn(b'Derived="1"', archive.read("Document.xml"))
