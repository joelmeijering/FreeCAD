// SPDX-License-Identifier: LGPL-2.1-or-later

#include "DerivedData.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

#include <QByteArrayView>
#include <QCryptographicHash>
#include <QCoreApplication>
#include <QFile>

#include <Base/Console.h>
#include <Base/Interpreter.h>
#include <Base/Uuid.h>

#include "Application.h"
#include "Document.h"
#include "DocumentObject.h"
#include "ElementNamingUtils.h"
#include "Property.h"
#include "PropertyLinks.h"
#include "PropertyPythonObject.h"
#include "PropertyStandard.h"

namespace fs = std::filesystem;

namespace
{

constexpr std::string_view cacheMarker {"FreeCADDerivedCache"};
constexpr int cacheFormat = 3;

bool isGenerationToken(std::string_view value)
{
    if (value.size() != 36) {
        return false;
    }
    for (std::size_t index = 0; index < value.size(); ++index) {
        const bool separator = index == 8 || index == 13 || index == 18 || index == 23;
        if (separator ? value[index] != '-'
                      : std::isxdigit(static_cast<unsigned char>(value[index])) == 0) {
            return false;
        }
    }
    return true;
}

const std::unordered_set<std::string_view> generatedPartDesignTypes {
    "PartDesign::AdditiveBox",
    "PartDesign::AdditiveCone",
    "PartDesign::AdditiveCylinder",
    "PartDesign::AdditiveEllipsoid",
    "PartDesign::AdditiveHelix",
    "PartDesign::AdditiveLoft",
    "PartDesign::AdditivePipe",
    "PartDesign::AdditivePrism",
    "PartDesign::AdditiveSphere",
    "PartDesign::AdditiveTorus",
    "PartDesign::AdditiveWedge",
    "PartDesign::Boolean",
    "PartDesign::Box",
    "PartDesign::Chamfer",
    "PartDesign::Cone",
    "PartDesign::Cylinder",
    "PartDesign::Draft",
    "PartDesign::Ellipsoid",
    "PartDesign::FeatureBase",
    "PartDesign::Fillet",
    "PartDesign::Groove",
    "PartDesign::Hole",
    "PartDesign::LinearPattern",
    "PartDesign::Mirrored",
    "PartDesign::MultiTransform",
    "PartDesign::Pad",
    "PartDesign::Pocket",
    "PartDesign::PolarPattern",
    "PartDesign::Prism",
    "PartDesign::Revolution",
    "PartDesign::Scaled",
    "PartDesign::Sphere",
    "PartDesign::SubtractiveBox",
    "PartDesign::SubtractiveCone",
    "PartDesign::SubtractiveCylinder",
    "PartDesign::SubtractiveEllipsoid",
    "PartDesign::SubtractiveHelix",
    "PartDesign::SubtractiveLoft",
    "PartDesign::SubtractivePipe",
    "PartDesign::SubtractivePrism",
    "PartDesign::SubtractiveSphere",
    "PartDesign::SubtractiveTorus",
    "PartDesign::SubtractiveWedge",
    "PartDesign::Thickness",
    "PartDesign::Torus",
    "PartDesign::Wedge",
};

std::string sha256(std::string_view value)
{
    QCryptographicHash hash(QCryptographicHash::Sha256);
    hash.addData(QByteArrayView(value.data(), static_cast<qsizetype>(value.size())));
    return hash.result().toHex().constData();
}

std::string sha256File(const fs::path& path)
{
    QFile file(QString::fromStdString(path.string()));
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file)) {
        return {};
    }
    return hash.result().toHex().constData();
}

std::string canonicalPath(const std::string& fileName)
{
    try {
        return fs::weakly_canonical(fs::absolute(fs::path(fileName))).string();
    }
    catch (const std::exception&) {
        return fs::absolute(fs::path(fileName)).lexically_normal().string();
    }
}

std::string compatibilityDescription()
{
    const auto& config = App::Application::Config();
    auto value = [&config](const char* key) {
        const auto it = config.find(key);
        return it == config.end() ? std::string() : it->second;
    };
    return "FreeCAD-" + value("BuildVersionMajor") + "." + value("BuildVersionMinor") + "R"
        + value("BuildRevision") + "-DerivedCache-" + std::to_string(cacheFormat)
        + "-PartShape-2-TPN" + std::to_string(App::getSelectedUnderlyingHistoryAlgorithm());
}

fs::path documentCacheRoot(const App::Document& document, const std::string& fileName)
{
    const auto compatibility = sha256(compatibilityDescription());
    const auto pathHash = sha256(canonicalPath(fileName));
    return fs::path(App::Application::getUserCachePath()) / "derived"
        / ("v" + std::to_string(cacheFormat)) / compatibility / document.Uid.getValueStr() / pathHash;
}

fs::path cacheBase()
{
    return fs::path(App::Application::getUserCachePath()) / "derived";
}

bool isCachePath(const fs::path& path)
{
    const auto relative = path.lexically_normal().lexically_relative(cacheBase().lexically_normal());
    return !relative.empty() && !relative.is_absolute() && *relative.begin() != "..";
}

bool isSafeExistingCachePath(const fs::path& path)
{
    if (!isCachePath(path)) {
        return false;
    }

    std::error_code error;
    auto current = cacheBase();
    const auto relative = path.lexically_normal().lexically_relative(current.lexically_normal());
    for (const auto& component : fs::path(".") / relative) {
        if (component != ".") {
            current /= component;
        }
        const auto status = fs::symlink_status(current, error);
        if (error) {
            if (error == std::errc::no_such_file_or_directory) {
                error.clear();
                continue;
            }
            return false;
        }
        if (fs::is_symlink(status)) {
            return false;
        }
    }
    return true;
}

void createPrivateDirectories(const fs::path& path)
{
    if (!isSafeExistingCachePath(path)) {
        throw std::runtime_error("unsafe derived-data cache path");
    }
    fs::create_directories(path);
    if (!isSafeExistingCachePath(path)) {
        throw std::runtime_error("unsafe derived-data cache directory");
    }

    auto current = cacheBase();
    const auto relative = path.lexically_normal().lexically_relative(current.lexically_normal());
    for (const auto& component : fs::path(".") / relative) {
        if (component != ".") {
            current /= component;
        }
        std::error_code error;
        fs::permissions(current, fs::perms::owner_all, fs::perm_options::replace, error);
        if (error) {
            throw std::runtime_error("cannot restrict derived-data cache permissions");
        }
    }
}

bool isRegularCacheFile(const fs::path& path)
{
    if (!isSafeExistingCachePath(path)) {
        return false;
    }
    std::error_code error;
    const auto status = fs::symlink_status(path, error);
    return !error && fs::is_regular_file(status);
}

void restrictPayloadPermissions(const fs::path& root)
{
    for (const auto& entry : fs::recursive_directory_iterator(root)) {
        std::error_code error;
        const auto status = entry.symlink_status(error);
        if (error || fs::is_symlink(status)) {
            throw std::runtime_error("unsafe derived-data payload path");
        }
        const auto permissions = fs::is_directory(status)
            ? fs::perms::owner_all
            : fs::perms::owner_read | fs::perms::owner_write;
        fs::permissions(entry.path(), permissions, fs::perm_options::replace, error);
        if (error) {
            throw std::runtime_error("cannot restrict derived-data payload permissions");
        }
    }
}

fs::path savedGenerationRoot(const App::Document& document,
                             const std::string& fileName,
                             const std::string& generation)
{
    return documentCacheRoot(document, fileName) / "saved" / generation;
}

fs::path workingRoot(const App::Document& document, const std::string& fileName)
{
    return documentCacheRoot(document, fileName) / "working"
        / std::to_string(QCoreApplication::applicationPid()) / "current";
}

bool hasLinks(const App::DocumentObject& object, const char* propertyName)
{
    const auto* rawProperty = object.getPropertyByName(propertyName);
    if (const auto* property = dynamic_cast<const App::PropertyXLinkSubList*>(rawProperty)) {
        return property->getSize() > 0;
    }
    if (const auto* property = dynamic_cast<const App::PropertyLinkSubList*>(rawProperty)) {
        return property->getSize() > 0;
    }
    const auto* property = dynamic_cast<const App::PropertyLinkBase*>(rawProperty);
    if (!property) {
        return false;
    }
    std::vector<App::DocumentObject*> links;
    property->getLinks(links, true);
    return !links.empty();
}

bool hasNonEmptyString(const App::DocumentObject& object, const char* propertyName)
{
    const auto* property = dynamic_cast<const App::PropertyString*>(
        object.getPropertyByName(propertyName)
    );
    return property && !property->getStrValue().empty();
}

bool hasPythonProxyClass(
    const App::DocumentObject& object,
    std::string_view moduleName,
    std::string_view className
)
{
    const auto* proxy = dynamic_cast<const App::PropertyPythonObject*>(
        object.getPropertyByName("Proxy")
    );
    if (!proxy) {
        return false;
    }

    Base::PyGILStateLocker lock;
    try {
        const Py::Object value = proxy->getValue();
        if (!value.ptr() || value.isNone()) {
            return false;
        }
        const Py::Object proxyType = value.type();
        if (!proxyType.hasAttr("__module__") || !proxyType.hasAttr("__name__")) {
            return false;
        }
        return Py::String(proxyType.getAttr("__module__")).as_std_string() == moduleName
            && Py::String(proxyType.getAttr("__name__")).as_std_string() == className;
    }
    catch (Py::Exception&) {
        PyErr_Clear();
        return false;
    }
}

bool isFeatureSlice(const App::DocumentObject& object)
{
    return object.getTypeId().getName() == std::string_view("Part::FeaturePython")
        && hasPythonProxyClass(object, "BOPTools.SplitFeatures", "FeatureSlice");
}

bool isSliceApartChild(const App::DocumentObject& object)
{
    // Plane-based collision filters identify sections even when a recompute
    // changes the order of the slice's solids. Such filters, including chains
    // that intersect multiple selections, are as reproducible as index filters.
    std::unordered_set<const App::DocumentObject*> visited;
    const auto* current = &object;
    while (current && visited.insert(current).second) {
        if (current->getTypeId().getName() != std::string_view("Part::FeaturePython")
            || !hasPythonProxyClass(*current, "CompoundTools.CompoundFilter", "_CompoundFilter")) {
            return false;
        }
        const auto* base = dynamic_cast<const App::PropertyLink*>(
            current->getPropertyByName("Base")
        );
        const auto* filter = dynamic_cast<const App::PropertyEnumeration*>(
            current->getPropertyByName("FilterType")
        );
        if (!base || !base->getValue() || !filter
            || !(filter->isValue("specific items")
                 || (filter->isValue("collision-pass") && hasLinks(*current, "Stencil")))) {
            return false;
        }
        current = base->getValue();
        if (isFeatureSlice(*current)) {
            return true;
        }
    }
    return false;
}

bool isCandidate(const App::DocumentObject& object, const App::Property& property)
{
    const char* propertyName = property.getName();
    if (!propertyName) {
        return false;
    }

    const std::string_view type(object.getTypeId().getName());
    const std::string_view prop(propertyName);

    if (type == "Sketcher::SketchObject" || type == "Sketcher::SketchObjectSF") {
        return prop == "Shape" || prop == "InternalShape";
    }
    if (type == "PartDesign::Body") {
        return prop == "Shape" && hasLinks(object, "Tip");
    }
    if (type == "PartDesign::ShapeBinder") {
        return prop == "Shape" && hasLinks(object, "Support");
    }
    if (type == "PartDesign::SubShapeBinder") {
        const auto* mode = dynamic_cast<const App::PropertyEnumeration*>(
            object.getPropertyByName("BindMode")
        );
        return prop == "Shape" && mode && mode->getValue() == 0 && hasLinks(object, "Support");
    }
    if (prop == "Shape" && type == "Part::Part2DObjectPython"
        && hasPythonProxyClass(object, "draftobjects.shapestring", "ShapeString")) {
        return hasNonEmptyString(object, "String") && hasNonEmptyString(object, "FontFile");
    }
    if (prop == "Shape" && isFeatureSlice(object)) {
        return hasLinks(object, "Base") && hasLinks(object, "Tools");
    }
    if (prop == "Shape" && isSliceApartChild(object)) {
        return true;
    }
    return generatedPartDesignTypes.contains(type)
        && (prop == "Shape" || prop == "AddSubShape" || prop == "SuppressedShape");
}

std::unordered_set<const App::DocumentObject*> pinnedBySolidSubelements(const App::Document& document)
{
    std::unordered_set<const App::DocumentObject*> pinned;
    // TPN2 mapped names are reproducible from the authored feature history. Consumers can
    // therefore resolve their subelement links after the producer shape and map are regenerated.
    if (App::getSelectedHistoryAlgorithm() == App::HistoryAlgorithm::V2) {
        return pinned;
    }
    for (const auto* owner : document.getObjects()) {
        std::vector<App::Property*> properties;
        owner->getPropertyList(properties);
        for (const auto* property : properties) {
            const auto type = property->getTypeId();
            const bool isLinkSub = type.isDerivedFrom(App::PropertyLinkSub::getClassTypeId())
                || type.isDerivedFrom(App::PropertyLinkSubList::getClassTypeId())
                || type.isDerivedFrom(App::PropertyXLinkSub::getClassTypeId())
                || (type.isDerivedFrom(App::PropertyXLinkSubList::getClassTypeId())
                    && !type.isDerivedFrom(App::PropertyXLinkList::getClassTypeId()));
            if (!isLinkSub) {
                continue;
            }
            const auto* link = dynamic_cast<const App::PropertyLinkBase*>(property);
            if (!link) {
                continue;
            }
            std::vector<App::DocumentObject*> targets;
            std::vector<std::string> subelements;
            try {
                link->getLinks(targets, true, &subelements, true);
            }
            catch (...) {
                Base::Console().warning(
                    "Unable to audit subelement link %s.%s; retaining all candidate geometry.\n",
                    owner->getNameInDocument(),
                    property->getName());
                for (const auto* object : document.getObjects()) {
                    pinned.insert(object);
                }
                return pinned;
            }
            if (subelements.empty()) {
                continue;
            }
            bool hasSubelement = false;
            for (const auto& sub : subelements) {
                if (!sub.empty()) {
                    hasSubelement = true;
                    break;
                }
            }
            if (!hasSubelement) {
                continue;
            }
            for (const auto* target : targets) {
                if (!target) {
                    continue;
                }
                const std::string_view targetType(target->getTypeId().getName());
                // Stable sketch geometry references are part of the supported
                // regeneration contract. Solid topology is deliberately pinned.
                if (targetType != "Sketcher::SketchObject"
                    && targetType != "Sketcher::SketchObjectSF") {
                    pinned.insert(target);
                }
            }
        }
    }
    return pinned;
}

// TODO requireValue never high, remove that flag
std::vector<App::DerivedData::Item> classifiedItems(const App::Document& document,
                                                     bool requireValue,
                                                     bool acceptTouched = false)
{
    const auto pinned = pinnedBySolidSubelements(document);
    std::vector<App::DerivedData::Item> result;
    for (auto* object : document.getObjects()) {
        if (!object || object->isError() || (!acceptTouched && object->isTouched())
            || pinned.contains(object)) {
            continue;
        }
        std::vector<App::Property*> properties;
        object->getPropertyList(properties);
        for (auto* property : properties) {
            if (!property || !isCandidate(*object, *property) || !property->supportsDerivedData()
                || (requireValue && !property->hasDerivedData())) {
                continue;
            }
            result.push_back({object, property});
        }
    }
    return result;
}

std::string itemKey(const App::DerivedData::Item& item)
{
    std::string identity = item.object->getNameInDocument();
    identity.push_back('\0');
    identity += item.property->getName();
    return sha256(identity);
}

struct ManifestFile
{
    std::string name;
    std::uintmax_t size;
    std::string digest;
};

struct ManifestItem
{
    std::string objectName;
    std::string propertyName;
    std::string objectType;
    std::string propertyType;
    std::string key;
    std::vector<ManifestFile> files;
};

std::vector<ManifestFile> payloadFiles(const fs::path& itemDirectory)
{
    std::vector<ManifestFile> result;
    for (const auto& entry : fs::recursive_directory_iterator(itemDirectory)) {
        if (!entry.is_regular_file()) {
            continue;
        }
        auto relative = fs::relative(entry.path(), itemDirectory).generic_string();
        result.push_back({relative, entry.file_size(), sha256File(entry.path())});
    }
    std::sort(result.begin(), result.end(), [](const auto& left, const auto& right) {
        return left.name < right.name;
    });
    return result;
}

bool writeManifest(const fs::path& root,
                   const App::Document& document,
                   const std::string& fileName,
                   const std::string& generation,
                   const std::vector<ManifestItem>& items)
{
    std::ofstream stream(root / "Manifest.txt", std::ios::out | std::ios::trunc);
    stream << cacheMarker << ' ' << cacheFormat << '\n'
           << std::quoted(document.Uid.getValueStr()) << '\n'
           << std::quoted(sha256(canonicalPath(fileName))) << '\n'
           << std::quoted(generation) << '\n'
           << std::quoted(compatibilityDescription()) << '\n'
           << items.size() << '\n';
    for (const auto& item : items) {
        stream << std::quoted(item.objectName) << ' ' << std::quoted(item.propertyName) << ' '
               << std::quoted(item.objectType) << ' ' << std::quoted(item.propertyType) << ' '
               << std::quoted(item.key) << ' ' << item.files.size() << '\n';
        for (const auto& file : item.files) {
            stream << std::quoted(file.name) << ' ' << file.size << ' ' << std::quoted(file.digest)
                   << '\n';
        }
    }
    stream.close();
    return stream.good();
}

bool readManifest(const fs::path& root,
                  const App::Document& document,
                  const std::string& fileName,
                  const std::string& generation,
                  std::vector<ManifestItem>& items)
{
    if (!isSafeExistingCachePath(root) || !isRegularCacheFile(root / "complete")
        || !isRegularCacheFile(root / "Manifest.txt")) {
        return false;
    }

    std::ifstream stream(root / "Manifest.txt");
    std::string marker;
    int format = 0;
    std::string uid;
    std::string pathHash;
    std::string storedGeneration;
    std::string compatibility;
    std::size_t count = 0;
    stream >> marker >> format >> std::quoted(uid) >> std::quoted(pathHash)
        >> std::quoted(storedGeneration) >> std::quoted(compatibility) >> count;
    if (!stream || marker != cacheMarker || format != cacheFormat
        || uid != document.Uid.getValueStr() || pathHash != sha256(canonicalPath(fileName))
        || storedGeneration != generation || compatibility != compatibilityDescription()) {
        return false;
    }

    items.clear();
    items.reserve(count);
    for (std::size_t i = 0; i < count; ++i) {
        ManifestItem item;
        std::size_t fileCount = 0;
        stream >> std::quoted(item.objectName) >> std::quoted(item.propertyName)
            >> std::quoted(item.objectType) >> std::quoted(item.propertyType)
            >> std::quoted(item.key) >> fileCount;
        if (!stream) {
            return false;
        }
        item.files.reserve(fileCount);
        for (std::size_t j = 0; j < fileCount; ++j) {
            ManifestFile file;
            stream >> std::quoted(file.name) >> file.size >> std::quoted(file.digest);
            if (!stream) {
                return false;
            }
            item.files.push_back(std::move(file));
        }
        items.push_back(std::move(item));
    }
    return true;
}

bool verifyItemFiles(const fs::path& directory, const ManifestItem& item)
{
    for (const auto& file : item.files) {
        const fs::path relative(file.name);
        if (relative.is_absolute() || file.name.empty()) {
            return false;
        }
        const fs::path normalized = relative.lexically_normal();
        if (normalized.empty() || *normalized.begin() == "..") {
            return false;
        }
        const fs::path fullPath = directory / normalized;
        std::error_code error;
        if (!isRegularCacheFile(fullPath) || fs::file_size(fullPath, error) != file.size || error
            || sha256File(fullPath) != file.digest) {
            return false;
        }
    }
    return true;
}

bool writeCache(const App::Document& document,
                const std::string& fileName,
                const std::string& generation,
                const fs::path& target,
                bool replace)
{
    const auto tmp = target.parent_path()
        / ("." + target.filename().string() + ".tmp-" + App::DerivedData::newGeneration());
    try {
        createPrivateDirectories(target.parent_path());
        if (!isSafeExistingCachePath(target) || fs::exists(tmp)) {
            throw std::runtime_error("unsafe derived-data cache publication target");
        }
        createPrivateDirectories(tmp / "Objects");
        std::vector<ManifestItem> manifestItems;
        for (const auto& item : classifiedItems(document, false)) {
            const auto key = itemKey(item);
            const auto directory = tmp / "Objects" / key;
            createPrivateDirectories(directory);
            if (!item.property->saveDerivedData(directory.string())) {
                fs::remove_all(directory);
                continue;
            }
            manifestItems.push_back({std::string(item.object->getNameInDocument()),
                                     std::string(item.property->getName()),
                                     std::string(item.object->getTypeId().getName()),
                                     std::string(item.property->getTypeId().getName()),
                                     key,
                                     payloadFiles(directory)});
        }
        if (!writeManifest(tmp, document, fileName, generation, manifestItems)) {
            throw std::runtime_error("failed to write cache manifest");
        }
        std::ofstream complete(tmp / "complete", std::ios::out | std::ios::trunc);
        complete << "complete\n";
        complete.close();
        if (!complete.good()) {
            throw std::runtime_error("failed to publish cache completion marker");
        }
        restrictPayloadPermissions(tmp);

        if (replace && fs::exists(target)) {
            if (!isSafeExistingCachePath(target)) {
                throw std::runtime_error("unsafe existing derived-data cache target");
            }
            const auto old = target.parent_path()
                / ("." + target.filename().string() + ".old-" + App::DerivedData::newGeneration());
            fs::rename(target, old);
            try {
                fs::rename(tmp, target);
            }
            catch (...) {
                std::error_code ignored;
                fs::rename(old, target, ignored);
                throw;
            }
            fs::remove_all(old);
        }
        else {
            fs::rename(tmp, target);
        }
        return true;
    }
    catch (const std::exception& exception) {
        std::error_code ignored;
        fs::remove_all(tmp, ignored);
        Base::Console().warning("Failed to write derived-data cache: %s\n", exception.what());
        return false;
    }
}

}  // namespace

std::vector<App::DerivedData::Item> App::DerivedData::items(const Document& document,
                                                              bool requireValue)
{
    return classifiedItems(document, requireValue);
}

std::string App::DerivedData::newGeneration()
{
    return Base::Uuid::createUuid();
}

bool App::DerivedData::saveGeneration(const Document& document,
                                       const std::string& fileName,
                                       const std::string& generation)
{
    if (fileName.empty() || !isGenerationToken(generation)) {
        return false;
    }
    return writeCache(document,
                      fileName,
                      generation,
                      savedGenerationRoot(document, fileName, generation),
                      false);
}

App::DerivedData::RestoreResult App::DerivedData::restoreGeneration(
    Document& document,
    const std::string& fileName,
    const std::string& generation,
    const std::unordered_set<DocumentObject*>& serializedTouched,
    const std::unordered_set<DocumentObject*>& forcedMisses)
{
    RestoreResult result;
    auto classified = classifiedItems(document, false, true);
    std::erase_if(classified, [&serializedTouched](const Item& item) {
        return serializedTouched.contains(item.object) || item.property->hasDerivedData();
    });
    result.missing = classified;

    if (!isGenerationToken(generation)) {
        return result;
    }
    const auto root = savedGenerationRoot(document, fileName, generation);
    std::vector<ManifestItem> manifest;
    if (!readManifest(root, document, fileName, generation, manifest)) {
        return result;
    }

    std::unordered_map<std::string, const ManifestItem*> manifestByIdentity;
    for (const auto& item : manifest) {
        manifestByIdentity[item.objectName + '\0' + item.propertyName] = &item;
    }

    result.missing.clear();
    for (const auto& item : classified) {
        if (forcedMisses.contains(item.object)) {
            result.missing.push_back(item);
            continue;
        }
        const std::string identity = std::string(item.object->getNameInDocument()) + '\0'
            + item.property->getName();
        const auto found = manifestByIdentity.find(identity);
        if (found == manifestByIdentity.end()) {
            result.missing.push_back(item);
            continue;
        }
        const auto& stored = *found->second;
        const auto directory = root / "Objects" / stored.key;
        if (stored.key != itemKey(item) || stored.objectType != item.object->getTypeId().getName()
            || stored.propertyType != item.property->getTypeId().getName()
            || !verifyItemFiles(directory, stored)
            || !item.property->restoreDerivedData(directory.string())) {
            result.missing.push_back(item);
            continue;
        }
        result.restored.push_back(item);
    }
    return result;
}

bool App::DerivedData::refreshGeneration(const Document& document,
                                          const std::string& fileName,
                                          const std::string& generation)
{
    if (fileName.empty() || !isGenerationToken(generation)) {
        return false;
    }
    return writeCache(document,
                      fileName,
                      generation,
                      savedGenerationRoot(document, fileName, generation),
                      true);
}

bool App::DerivedData::saveWorking(const Document& document, const std::string& fileName)
{
    if (fileName.empty()) {
        return false;
    }
    return writeCache(document, fileName, "working", workingRoot(document, fileName), true);
}

void App::DerivedData::invalidateWorking(const Document& document, const std::string& fileName)
{
    if (fileName.empty()) {
        return;
    }
    const auto root = workingRoot(document, fileName);
    if (!isSafeExistingCachePath(root)) {
        Base::Console().warning("Refusing to remove unsafe derived-data working cache.\n");
        return;
    }
    std::error_code ignored;
    fs::remove_all(root, ignored);
}
